"""Acoustic impedance physical-realizability gate (#705,
REV58-DSPDECAY).

An acoustic impedance can be the right quantity with the right units
(#570) and still be physically or numerically inadmissible: imported,
interpolated or fitted complex responses can be non-passive, non-causal,
unstable, or unrealizable in the selected time-domain formulation. A
material model that is semantically correct but non-causal/non-passive/
unstable makes a sophisticated solver produce very precise nonphysical
results.

This module is the versioned, fail-closed realizability gate:

- :class:`CadBoundaryEvidenceRecord` — the pinned boundary evidence with
  its declared class (measured impedance / measured reflection /
  parametric physical model / inverse-fitted model / rational fit /
  state-space / IIR-ADE / active-control boundary / unknown), the
  impedance/admittance and normal-direction convention, the measured
  band, and the physical metrics the gate needs: minimum resistive value
  with its uncertainty, maximum reflection magnitude, interpolation and
  extrapolation declarations, causality-check outcome and
  conjugate-symmetry state. Original evidence is immutable — a repaired
  passive projection is a *new derived* artifact (#705 §10).

- :class:`CadBoundaryRationalFit` — a rational/vector-fit artifact with
  full provenance: input evidence pin, fit variable (Z / Y / reflection),
  fit band, weighting, pole count, initialization, algorithm+version,
  stable-pole and passivity constraints, regularization, residual
  content hash, extrapolation rule and the resulting poles/residues.
  Changing band/order/weighting creates a new realization — never an
  edit (#705 §8).

- :class:`CadTdImpedanceRealization` — the solver-facing time-domain
  realization: solver family, timestep, boundary update scheme,
  integration method, stability margin, FD↔TD residual envelope
  (magnitude/phase/reflection) and the long-time energy check (#705
  §13–§15).

- :class:`CadBoundaryRealizabilityAssessment` — the sealed per
  realization+domain verdict with the §19 machine states, plus the
  component sub-states (passivity class §4, causality §6, pole stability
  §9) so the gate never collapses them into one badge.

Gate ordering (#705 §1): this runs *after* #570 quantity/incidence
compatibility and *before* #683/#687 numerical validation and #566
measured validation. Passing #570 proves semantic compatibility only.

Literature basis
----------------
- Toyoda, *Stability conditions for impedance boundaries in the
  finite-difference time-domain method*, Acoustical Science and
  Technology 39(5), 2018 — boundary admissibility is not exhausted by
  the global free-field CFL condition; the boundary update carries its
  own stability constraint.
- Jang & Ih, *Stabilization of time domain acoustic boundary element
  method for the interior problem with impedance boundary conditions*,
  JASA 131, 2012 — converting fitted frequency-domain impedance to a
  time-domain IIR introduces modeling error and unstable time-marching
  modes.
- Zhong, Zhang & Huang, *A controllable canonical form implementation
  of time domain impedance boundary conditions for broadband
  aeroacoustic computation*, J. Comput. Phys. 313, 2016 — rational
  impedance models require poles in the stable half-plane or the
  time-domain realization loses causality/stability.
- Wang & Hornikx, *Time-domain impedance boundary condition modeling
  with the discontinuous Galerkin method for room acoustics
  simulations*, JASA 147, 2020 — the rational approximation itself is
  part of boundary-model identity.
- Rodio, Hu & Nark, *Time-Domain Boundary Element Method with Broadband
  Impedance Boundary Condition*, AIAA Journal, 2022 — coupled-system
  stability by eigenvalue analysis, not assumed from a plausible fit.
- Srivastava, *Causality and passivity: From electromagnetism and
  network theory to metamaterials*, Mechanics of Materials 154, 2021 —
  real and imaginary parts are not arbitrary independent curves for a
  passive causal system; finite-band data do not justify a naïve global
  Kramers–Kronig pass/fail.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


REALIZ_AUTHORITY_SCHEMA_VERSION = 'realiz-boundary-1'
REALIZ_EVALUATION_VERSION = 'realiz-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Default passive reflection bound under the declared convention;
#: overridable per assessment call because the bound depends on
#: impedance/admittance convention and incidence (#705 §3).
_DEFAULT_PASSIVE_REFLECTION_BOUND = 1.0


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


# ---------------------------------------------------------------------------
# Taxonomies (#705)
# ---------------------------------------------------------------------------

BoundaryEvidenceClass = Literal[
    'measured_frequency_domain_impedance',
    'measured_complex_reflection',
    'parametric_physical_model',
    'inverse_fitted_physical_model',
    'rational_frequency_domain_fit',
    'state_space_time_domain_realization',
    'iir_ade_realization',
    'active_control_boundary',
    'unknown',
]

BoundaryQuantityConvention = Literal[
    'impedance_z',
    'admittance_y',
    'reflection_coefficient',
    'other',
    'unknown',
]

BoundaryPassivityClass = Literal[
    'passive_boundary',
    'active_boundary_explicit',
    'nonpassive_unexpected',
    'unknown',
]

BoundaryCausalityState = Literal[
    'causal_by_physical_parametric_model',
    'causal_by_stable_rational_realization',
    'causality_supported_with_limitations',
    'causality_unresolved_finite_band',
    'causality_violation_detected',
    'unknown',
]

BoundaryStabilityState = Literal[
    'stable_realization',
    'marginally_stable_review',
    'unstable_realization',
    'stability_unknown',
]

BoundaryInterpolationKind = Literal[
    'none',
    'declared_causal_method',
    'independent_component_splines',
    'declared_external',
    'unknown',
]

BoundaryExtrapolationKind = Literal[
    'not_required',
    'declared_physical_asymptote',
    'constant_endpoint',
    'arbitrary_endpoint',
    'unknown',
]

BoundaryCausalityCheck = Literal[
    'not_performed',
    'consistent_within_band',
    'inconsistent',
    'unknown',
]

ConjugateSymmetryState = Literal[
    'satisfied',
    'violated',
    'not_applicable',
    'unknown',
]

RationalFitVariable = Literal[
    'impedance_z',
    'admittance_y',
    'reflection_coefficient',
    'other',
]

RationalFitForm = Literal[
    'rational_transfer',
    'state_space',
    'iir',
    'auxiliary_differential_equation',
]

RationalStablePoleConstraint = Literal[
    'enforced_during_fit',
    'post_fit_check',
    'none',
    'unknown',
]

RationalPassivityEnforcement = Literal[
    'none',
    'enforced_during_fit',
    'repaired_post_fit',
    'unknown',
]

RealizationTimeDomain = Literal[
    'continuous',
    'discrete',
]

TdSolverFamily = Literal[
    'fdtd',
    'fdtd_impedance_boundary',
    'td_dg',
    'td_bem',
    'iir_ade',
    'state_space',
    'other',
    'unknown',
]

BoundaryRealizabilityState = Literal[
    'passive_causal_validated',
    'passive_with_limitations',
    'passivity_unresolved_with_uncertainty',
    'causality_unresolved_finite_band',
    'stable_numerical_realization',
    'nonpassive_input',
    'unstable_fit',
    'time_domain_realization_mismatch',
    'active_boundary_explicit',
    'insufficient_evidence',
]

#: Separate uncertainty components kept by the assessment (#705 §18) —
#: boundary-realization error never collapses into one material error.
BoundaryErrorComponent = Literal[
    'measurement_impedance',
    'phase_time_reference',
    'fit_model',
    'passivity_repair_delta',
    'out_of_band_assumption',
    'td_realization_error',
    'solver_discretization',
]


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadRationalPole(BaseModel):
    """One pole of a rational/state-space realization (#705 §8/§9)."""

    model_config = ConfigDict(frozen=True)

    re: float
    im: float = 0.0

    @model_validator(mode='after')
    def valid_pole(self) -> 'CadRationalPole':
        _require_finite(self.re, 'pole real part')
        _require_finite(self.im, 'pole imaginary part')
        return self

    def is_stable(
        self, domain: RealizationTimeDomain, margin: float
    ) -> Literal['stable', 'marginal', 'unstable']:
        if domain == 'continuous':
            if self.re < -margin:
                return 'stable'
            if self.re <= margin:
                return 'marginal'
            return 'unstable'
        magnitude = sqrt(self.re * self.re + self.im * self.im)
        if magnitude < 1.0 - margin:
            return 'stable'
        if magnitude <= 1.0 + margin:
            return 'marginal'
        return 'unstable'


class CadBoundaryUncertaintyBreakdown(BaseModel):
    """The declared uncertainty contributions for one evidence record
    (#705 §5/§18) — kept separate so a passivity decision never silently
    borrows confidence from an unmeasured component."""

    model_config = ConfigDict(frozen=True)

    resistive_uncertainty: float | None = None
    reflection_uncertainty: float | None = None
    phase_reference_uncertainty_deg: float | None = None
    additional_components: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_breakdown(self) -> 'CadBoundaryUncertaintyBreakdown':
        for label, value in (
            ('resistive_uncertainty', self.resistive_uncertainty),
            ('reflection_uncertainty', self.reflection_uncertainty),
            (
                'phase_reference_uncertainty_deg',
                self.phase_reference_uncertainty_deg,
            ),
        ):
            if value is not None:
                _require_finite(value, f'boundary uncertainty {label}')
                if value < 0:
                    raise ValueError(
                        f'boundary uncertainty {label} must be '
                        'non-negative'
                    )
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadBoundaryEvidenceRecord(BaseModel):
    """Sealed pinned boundary evidence with physical metrics (#705 §2–§7).

    ``boundary_class`` keeps measured data, parametric models, fitted
    artifacts and active-control boundaries distinct — a fitted transfer
    model is a derived artifact, never the original measurement.
    ``quantity_convention`` + ``normal_direction`` carry the convention
    the passivity metrics were evaluated under; ``min_resistive_value``
    is the minimum Re(Z) (or the equivalent dissipative term in the
    declared convention) and is never clipped on input — a negative value
    is evidence to classify, not noise to erase (#705 §5).
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    evidence_label: str = Field(min_length=1)
    boundary_class: BoundaryEvidenceClass
    quantity_convention: BoundaryQuantityConvention
    normal_direction: Literal[
        'into_boundary', 'into_domain', 'other', 'unknown'
    ] = 'unknown'
    passivity_class: BoundaryPassivityClass = 'unknown'
    source_ref: AuthorityRef | None = None
    derived_from_ref: AuthorityRef | None = None
    measured_band_low_hz: float | None = None
    measured_band_high_hz: float | None = None
    min_resistive_value: float | None = None
    max_reflection_magnitude: float | None = None
    uncertainty: CadBoundaryUncertaintyBreakdown | None = None
    interpolation: BoundaryInterpolationKind = 'unknown'
    extrapolation: BoundaryExtrapolationKind = 'unknown'
    low_frequency_asymptote: str | None = None
    high_frequency_asymptote: str | None = None
    causality_check: BoundaryCausalityCheck = 'unknown'
    causality_check_band_hz: tuple[float, float] | None = None
    out_of_band_assumption: str | None = None
    conjugate_symmetry: ConjugateSymmetryState = 'unknown'
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_record(self) -> 'CadBoundaryEvidenceRecord':
        _require_iso8601(
            self.declared_at_utc, 'boundary evidence declared_at_utc'
        )
        for label, ref in (
            ('source_ref', self.source_ref),
            ('derived_from_ref', self.derived_from_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        for label, value in (
            ('measured_band_low_hz', self.measured_band_low_hz),
            ('measured_band_high_hz', self.measured_band_high_hz),
            ('min_resistive_value', self.min_resistive_value),
            ('max_reflection_magnitude', self.max_reflection_magnitude),
        ):
            if value is not None:
                _require_finite(value, f'boundary evidence {label}')
        if (
            self.measured_band_low_hz is not None
            and self.measured_band_high_hz is not None
            and self.measured_band_high_hz <= self.measured_band_low_hz
        ):
            raise ValueError(
                'measured band edges must be ascending'
            )
        if self.causality_check_band_hz is not None:
            lo, hi = self.causality_check_band_hz
            _require_finite(lo, 'causality check band low')
            _require_finite(hi, 'causality check band high')
            if hi <= lo:
                raise ValueError(
                    'causality check band edges must be ascending'
                )
        if (
            self.boundary_class == 'active_control_boundary'
            and self.passivity_class not in (
                'active_boundary_explicit', 'unknown'
            )
        ):
            raise ValueError(
                'an active-control boundary must declare '
                'active_boundary_explicit — it cannot enter through a '
                'passive-material path (#705 §4)'
            )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('boundary evidence record hash mismatch')
        if self.record_id != _semantic_id('bdevi', expected):
            raise ValueError(
                'boundary evidence record id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'evidence_label': self.evidence_label,
            'boundary_class': self.boundary_class,
            'quantity_convention': self.quantity_convention,
            'normal_direction': self.normal_direction,
            'passivity_class': self.passivity_class,
            'source_ref': (
                self.source_ref.model_dump(mode='json')
                if self.source_ref is not None
                else None
            ),
            'derived_from_ref': (
                self.derived_from_ref.model_dump(mode='json')
                if self.derived_from_ref is not None
                else None
            ),
            'measured_band_low_hz': self.measured_band_low_hz,
            'measured_band_high_hz': self.measured_band_high_hz,
            'min_resistive_value': self.min_resistive_value,
            'max_reflection_magnitude': self.max_reflection_magnitude,
            'uncertainty': (
                self.uncertainty.model_dump(mode='json')
                if self.uncertainty is not None
                else None
            ),
            'interpolation': self.interpolation,
            'extrapolation': self.extrapolation,
            'low_frequency_asymptote': self.low_frequency_asymptote,
            'high_frequency_asymptote': self.high_frequency_asymptote,
            'causality_check': self.causality_check,
            'causality_check_band_hz': (
                list(self.causality_check_band_hz)
                if self.causality_check_band_hz is not None
                else None
            ),
            'out_of_band_assumption': self.out_of_band_assumption,
            'conjugate_symmetry': self.conjugate_symmetry,
            'content_sha256': self.content_sha256,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def boundary_evidence_binding(
    record: CadBoundaryEvidenceRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='boundary_evidence_record',
        ref_id=record.record_id,
        ref_sha256=record.record_sha256,
    )


class CadBoundaryRationalFit(BaseModel):
    """Sealed rational/vector-fit artifact (#705 §8–§10).

    Full fit provenance — input evidence pin, fit variable, band,
    weighting, pole order, initialization, algorithm+version, stable-pole
    and passivity constraints, regularization, residual content hash and
    extrapolation rule — plus the resulting poles/residues. Only final
    coefficients are never enough.
    """

    model_config = ConfigDict(frozen=True)

    fit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    input_evidence_ref: AuthorityRef
    fit_variable: RationalFitVariable
    fit_form: RationalFitForm = 'rational_transfer'
    time_domain: RealizationTimeDomain = 'continuous'
    fit_band_low_hz: float
    fit_band_high_hz: float
    weighting: str = 'unknown'
    pole_count: int
    initialization: str | None = None
    algorithm: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    stable_pole_constraint: RationalStablePoleConstraint
    passivity_enforcement: RationalPassivityEnforcement = 'none'
    regularization: str | None = None
    residual_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    max_residual_db: float | None = None
    extrapolation_rule: str | None = None
    poles: tuple[CadRationalPole, ...] = ()
    residues: tuple[tuple[float, float], ...] = ()
    repair_delta: float | None = None
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    fit_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_fit(self) -> 'CadBoundaryRationalFit':
        _require_iso8601(
            self.declared_at_utc, 'rational fit declared_at_utc'
        )
        if self.input_evidence_ref.ref_sha256 is None:
            raise ValueError(
                'input_evidence_ref must carry its sha256 pin'
            )
        _require_finite(self.fit_band_low_hz, 'fit band low')
        _require_finite(self.fit_band_high_hz, 'fit band high')
        if self.fit_band_high_hz <= self.fit_band_low_hz:
            raise ValueError('fit band edges must be ascending')
        if self.pole_count < 0:
            raise ValueError('pole_count must be non-negative')
        if self.poles and len(self.poles) != self.pole_count:
            raise ValueError(
                'pole_count must equal the number of declared poles'
            )
        if self.max_residual_db is not None:
            _require_finite(self.max_residual_db, 'max_residual_db')
        if self.repair_delta is not None:
            _require_finite(self.repair_delta, 'repair_delta')
            if self.repair_delta < 0:
                raise ValueError('repair_delta must be non-negative')
        if self.residues and len(self.residues) != self.pole_count:
            raise ValueError(
                'residues must pair one-to-one with poles'
            )
        expected = _hash(self.identity_payload())
        if self.fit_sha256 != expected:
            raise ValueError('boundary rational fit hash mismatch')
        if self.fit_id != _semantic_id('bdrat', expected):
            raise ValueError(
                'boundary rational fit id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'input_evidence_ref': self.input_evidence_ref.model_dump(
                mode='json'
            ),
            'fit_variable': self.fit_variable,
            'fit_form': self.fit_form,
            'time_domain': self.time_domain,
            'fit_band_low_hz': self.fit_band_low_hz,
            'fit_band_high_hz': self.fit_band_high_hz,
            'weighting': self.weighting,
            'pole_count': self.pole_count,
            'initialization': self.initialization,
            'algorithm': self.algorithm,
            'algorithm_version': self.algorithm_version,
            'stable_pole_constraint': self.stable_pole_constraint,
            'passivity_enforcement': self.passivity_enforcement,
            'regularization': self.regularization,
            'residual_sha256': self.residual_sha256,
            'max_residual_db': self.max_residual_db,
            'extrapolation_rule': self.extrapolation_rule,
            'poles': [p.model_dump(mode='json') for p in self.poles],
            'residues': [list(r) for r in self.residues],
            'repair_delta': self.repair_delta,
            'content_sha256': self.content_sha256,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def pole_stability(
        self, margin: float = 0.0
    ) -> BoundaryStabilityState:
        """Deterministic pole-stability classification (#705 §9).

        Continuous-time poles are stable strictly left of the imaginary
        axis; discrete-time poles inside the unit circle. Marginal poles
        inside ``margin`` require review — never silently promoted.
        """
        if not self.poles:
            return 'stability_unknown'
        worst = 'stable'
        for pole in self.poles:
            verdict = pole.is_stable(self.time_domain, margin)
            if verdict == 'unstable':
                return 'unstable_realization'
            if verdict == 'marginal':
                worst = 'marginally_stable_review'
        return worst if worst == 'marginally_stable_review' else (
            'stable_realization'
        )


def boundary_rational_fit_binding(
    fit: CadBoundaryRationalFit,
) -> AuthorityRef:
    return AuthorityRef(
        kind='boundary_rational_fit',
        ref_id=fit.fit_id,
        ref_sha256=fit.fit_sha256,
    )


class CadTdImpedanceRealization(BaseModel):
    """Sealed solver-facing time-domain realization (#705 §13–§15).

    Physical passivity does not prove numerical stability for every
    discretization: the boundary update scheme, timestep, integration
    method and stability margin are pinned separately from free-field
    CFL evidence (#683), and the FD↔TD cross-check residual envelope
    (``fd_magnitude_residual_db`` / ``fd_phase_residual_deg`` /
    ``fd_reflection_residual``) plus the long-time energy check are part
    of the realization identity.
    """

    model_config = ConfigDict(frozen=True)

    realization_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    evidence_ref: AuthorityRef
    rational_fit_ref: AuthorityRef | None = None
    solver_family: TdSolverFamily
    timestep_s: float | None = None
    boundary_update_scheme: str | None = None
    integration_method: str | None = None
    boundary_stability_margin: float | None = None
    boundary_stability_criterion: str | None = None
    cfl_evidence_ref: AuthorityRef | None = None
    solver_band_low_hz: float | None = None
    solver_band_high_hz: float | None = None
    fd_magnitude_residual_db: float | None = None
    fd_phase_residual_deg: float | None = None
    fd_reflection_residual: float | None = None
    residual_tolerance: float | None = None
    energy_growth_observed: bool | None = None
    energy_growth_ratio: float | None = None
    conjugate_symmetry: ConjugateSymmetryState = 'unknown'
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    realization_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_realization(self) -> 'CadTdImpedanceRealization':
        _require_iso8601(
            self.declared_at_utc, 'td realization declared_at_utc'
        )
        for label, ref in (
            ('evidence_ref', self.evidence_ref),
            ('rational_fit_ref', self.rational_fit_ref),
            ('cfl_evidence_ref', self.cfl_evidence_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        for label, value in (
            ('timestep_s', self.timestep_s),
            ('boundary_stability_margin', self.boundary_stability_margin),
            ('solver_band_low_hz', self.solver_band_low_hz),
            ('solver_band_high_hz', self.solver_band_high_hz),
            ('fd_magnitude_residual_db', self.fd_magnitude_residual_db),
            ('fd_phase_residual_deg', self.fd_phase_residual_deg),
            ('fd_reflection_residual', self.fd_reflection_residual),
            ('residual_tolerance', self.residual_tolerance),
            ('energy_growth_ratio', self.energy_growth_ratio),
        ):
            if value is not None:
                _require_finite(value, f'td realization {label}')
        if self.timestep_s is not None and self.timestep_s <= 0:
            raise ValueError('timestep_s must be positive')
        if (
            self.solver_band_low_hz is not None
            and self.solver_band_high_hz is not None
            and self.solver_band_high_hz <= self.solver_band_low_hz
        ):
            raise ValueError('solver band edges must be ascending')
        if self.residual_tolerance is not None and (
            self.residual_tolerance < 0
        ):
            raise ValueError('residual_tolerance must be non-negative')
        expected = _hash(self.identity_payload())
        if self.realization_sha256 != expected:
            raise ValueError('td impedance realization hash mismatch')
        if self.realization_id != _semantic_id('bdtim', expected):
            raise ValueError(
                'td impedance realization id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'evidence_ref': self.evidence_ref.model_dump(mode='json'),
            'rational_fit_ref': (
                self.rational_fit_ref.model_dump(mode='json')
                if self.rational_fit_ref is not None
                else None
            ),
            'solver_family': self.solver_family,
            'timestep_s': self.timestep_s,
            'boundary_update_scheme': self.boundary_update_scheme,
            'integration_method': self.integration_method,
            'boundary_stability_margin': self.boundary_stability_margin,
            'boundary_stability_criterion':
                self.boundary_stability_criterion,
            'cfl_evidence_ref': (
                self.cfl_evidence_ref.model_dump(mode='json')
                if self.cfl_evidence_ref is not None
                else None
            ),
            'solver_band_low_hz': self.solver_band_low_hz,
            'solver_band_high_hz': self.solver_band_high_hz,
            'fd_magnitude_residual_db': self.fd_magnitude_residual_db,
            'fd_phase_residual_deg': self.fd_phase_residual_deg,
            'fd_reflection_residual': self.fd_reflection_residual,
            'residual_tolerance': self.residual_tolerance,
            'energy_growth_observed': self.energy_growth_observed,
            'energy_growth_ratio': self.energy_growth_ratio,
            'conjugate_symmetry': self.conjugate_symmetry,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def td_realization_binding(
    realization: CadTdImpedanceRealization,
) -> AuthorityRef:
    return AuthorityRef(
        kind='td_impedance_realization',
        ref_id=realization.realization_id,
        ref_sha256=realization.realization_sha256,
    )


class CadBoundaryRealizabilityAssessment(BaseModel):
    """Sealed per-realization+domain realizability verdict (#705 §19).

    ``state`` is the machine-readable capability state;
    ``passivity_class`` / ``causality_state`` / ``stability_state`` keep
    the component verdicts visible instead of collapsing them into one
    badge; ``error_components`` records which uncertainty contributions
    apply (#705 §18).
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    evidence_ref: AuthorityRef
    rational_fit_ref: AuthorityRef | None = None
    td_realization_ref: AuthorityRef | None = None
    solver_band_low_hz: float | None = None
    solver_band_high_hz: float | None = None
    state: BoundaryRealizabilityState
    passivity_class: BoundaryPassivityClass
    causality_state: BoundaryCausalityState
    stability_state: BoundaryStabilityState
    solver_band_within_evidence: bool | None = None
    error_components: tuple[BoundaryErrorComponent, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadBoundaryRealizabilityAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        for label, ref in (
            ('evidence_ref', self.evidence_ref),
            ('rational_fit_ref', self.rational_fit_ref),
            ('td_realization_ref', self.td_realization_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if (
            self.solver_band_low_hz is not None
            and self.solver_band_high_hz is not None
            and self.solver_band_high_hz <= self.solver_band_low_hz
        ):
            raise ValueError(
                'assessment solver band edges must be ascending'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError(
                'boundary realizability assessment hash mismatch'
            )
        if self.assessment_id != _semantic_id('bdass', expected):
            raise ValueError(
                'boundary realizability assessment id does not match '
                'its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'evidence_ref': self.evidence_ref.model_dump(mode='json'),
            'rational_fit_ref': (
                self.rational_fit_ref.model_dump(mode='json')
                if self.rational_fit_ref is not None
                else None
            ),
            'td_realization_ref': (
                self.td_realization_ref.model_dump(mode='json')
                if self.td_realization_ref is not None
                else None
            ),
            'solver_band_low_hz': self.solver_band_low_hz,
            'solver_band_high_hz': self.solver_band_high_hz,
            'state': self.state,
            'passivity_class': self.passivity_class,
            'causality_state': self.causality_state,
            'stability_state': self.stability_state,
            'solver_band_within_evidence': self.solver_band_within_evidence,
            'error_components': list(self.error_components),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_boundary_evidence_record(
    *,
    document_id: str,
    evidence_label: str,
    boundary_class: BoundaryEvidenceClass,
    quantity_convention: BoundaryQuantityConvention,
    content_sha256: str,
    normal_direction: Literal[
        'into_boundary', 'into_domain', 'other', 'unknown'
    ] = 'unknown',
    passivity_class: BoundaryPassivityClass = 'unknown',
    source_ref: AuthorityRef | None = None,
    derived_from_ref: AuthorityRef | CadBoundaryEvidenceRecord
    | None = None,
    measured_band_low_hz: float | None = None,
    measured_band_high_hz: float | None = None,
    min_resistive_value: float | None = None,
    max_reflection_magnitude: float | None = None,
    uncertainty: CadBoundaryUncertaintyBreakdown | None = None,
    interpolation: BoundaryInterpolationKind = 'unknown',
    extrapolation: BoundaryExtrapolationKind = 'unknown',
    low_frequency_asymptote: str | None = None,
    high_frequency_asymptote: str | None = None,
    causality_check: BoundaryCausalityCheck = 'unknown',
    causality_check_band_hz: tuple[float, float] | None = None,
    out_of_band_assumption: str | None = None,
    conjugate_symmetry: ConjugateSymmetryState = 'unknown',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadBoundaryEvidenceRecord:
    """Seal pinned boundary evidence (never clipped)."""
    if isinstance(derived_from_ref, CadBoundaryEvidenceRecord):
        derived_from_ref = boundary_evidence_binding(derived_from_ref)
    payload = dict(
        document_id=document_id,
        evidence_label=evidence_label,
        boundary_class=boundary_class,
        quantity_convention=quantity_convention,
        normal_direction=normal_direction,
        passivity_class=passivity_class,
        source_ref=source_ref,
        derived_from_ref=derived_from_ref,
        measured_band_low_hz=measured_band_low_hz,
        measured_band_high_hz=measured_band_high_hz,
        min_resistive_value=min_resistive_value,
        max_reflection_magnitude=max_reflection_magnitude,
        uncertainty=uncertainty,
        interpolation=interpolation,
        extrapolation=extrapolation,
        low_frequency_asymptote=low_frequency_asymptote,
        high_frequency_asymptote=high_frequency_asymptote,
        causality_check=causality_check,
        causality_check_band_hz=causality_check_band_hz,
        out_of_band_assumption=out_of_band_assumption,
        conjugate_symmetry=conjugate_symmetry,
        content_sha256=content_sha256,
        authority_version=REALIZ_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadBoundaryEvidenceRecord, payload,
        'record_id', 'record_sha256', 'bdevi',
    )


def build_boundary_rational_fit(
    *,
    document_id: str,
    input_evidence: CadBoundaryEvidenceRecord | AuthorityRef,
    fit_variable: RationalFitVariable,
    fit_band_low_hz: float,
    fit_band_high_hz: float,
    pole_count: int,
    algorithm: str,
    algorithm_version: str,
    stable_pole_constraint: RationalStablePoleConstraint,
    content_sha256: str,
    fit_form: RationalFitForm = 'rational_transfer',
    time_domain: RealizationTimeDomain = 'continuous',
    weighting: str = 'unknown',
    initialization: str | None = None,
    passivity_enforcement: RationalPassivityEnforcement = 'none',
    regularization: str | None = None,
    residual_sha256: str | None = None,
    max_residual_db: float | None = None,
    extrapolation_rule: str | None = None,
    poles: tuple[CadRationalPole, ...] | list[CadRationalPole] = (),
    residues: tuple[tuple[float, float], ...]
    | list[tuple[float, float]] = (),
    repair_delta: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadBoundaryRationalFit:
    """Seal a rational/vector-fit artifact with full provenance."""
    if isinstance(input_evidence, CadBoundaryEvidenceRecord):
        input_evidence = boundary_evidence_binding(input_evidence)
    payload = dict(
        document_id=document_id,
        input_evidence_ref=input_evidence,
        fit_variable=fit_variable,
        fit_form=fit_form,
        time_domain=time_domain,
        fit_band_low_hz=fit_band_low_hz,
        fit_band_high_hz=fit_band_high_hz,
        weighting=weighting,
        pole_count=pole_count,
        initialization=initialization,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        stable_pole_constraint=stable_pole_constraint,
        passivity_enforcement=passivity_enforcement,
        regularization=regularization,
        residual_sha256=residual_sha256,
        max_residual_db=max_residual_db,
        extrapolation_rule=extrapolation_rule,
        poles=tuple(poles),
        residues=tuple(tuple(r) for r in residues),
        repair_delta=repair_delta,
        content_sha256=content_sha256,
        authority_version=REALIZ_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadBoundaryRationalFit, payload,
        'fit_id', 'fit_sha256', 'bdrat',
    )


def build_td_impedance_realization(
    *,
    document_id: str,
    evidence: CadBoundaryEvidenceRecord | AuthorityRef,
    solver_family: TdSolverFamily,
    rational_fit: CadBoundaryRationalFit | AuthorityRef | None = None,
    timestep_s: float | None = None,
    boundary_update_scheme: str | None = None,
    integration_method: str | None = None,
    boundary_stability_margin: float | None = None,
    boundary_stability_criterion: str | None = None,
    cfl_evidence_ref: AuthorityRef | None = None,
    solver_band_low_hz: float | None = None,
    solver_band_high_hz: float | None = None,
    fd_magnitude_residual_db: float | None = None,
    fd_phase_residual_deg: float | None = None,
    fd_reflection_residual: float | None = None,
    residual_tolerance: float | None = None,
    energy_growth_observed: bool | None = None,
    energy_growth_ratio: float | None = None,
    conjugate_symmetry: ConjugateSymmetryState = 'unknown',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadTdImpedanceRealization:
    """Seal a solver-facing TD realization."""
    if isinstance(evidence, CadBoundaryEvidenceRecord):
        evidence = boundary_evidence_binding(evidence)
    if isinstance(rational_fit, CadBoundaryRationalFit):
        rational_fit = boundary_rational_fit_binding(rational_fit)
    payload = dict(
        document_id=document_id,
        evidence_ref=evidence,
        rational_fit_ref=rational_fit,
        solver_family=solver_family,
        timestep_s=timestep_s,
        boundary_update_scheme=boundary_update_scheme,
        integration_method=integration_method,
        boundary_stability_margin=boundary_stability_margin,
        boundary_stability_criterion=boundary_stability_criterion,
        cfl_evidence_ref=cfl_evidence_ref,
        solver_band_low_hz=solver_band_low_hz,
        solver_band_high_hz=solver_band_high_hz,
        fd_magnitude_residual_db=fd_magnitude_residual_db,
        fd_phase_residual_deg=fd_phase_residual_deg,
        fd_reflection_residual=fd_reflection_residual,
        residual_tolerance=residual_tolerance,
        energy_growth_observed=energy_growth_observed,
        energy_growth_ratio=energy_growth_ratio,
        conjugate_symmetry=conjugate_symmetry,
        authority_version=REALIZ_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadTdImpedanceRealization, payload,
        'realization_id', 'realization_sha256', 'bdtim',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_boundary_realizability(
    *,
    document_id: str,
    evidence: CadBoundaryEvidenceRecord,
    rational_fit: CadBoundaryRationalFit | None = None,
    td_realization: CadTdImpedanceRealization | None = None,
    solver_band_low_hz: float | None = None,
    solver_band_high_hz: float | None = None,
    passive_reflection_bound: float = (
        _DEFAULT_PASSIVE_REFLECTION_BOUND
    ),
    pole_stability_margin: float = 0.0,
    evaluated_at_utc: str | None = None,
) -> CadBoundaryRealizabilityAssessment:
    """Fail-closed physical-realizability verdict (#705 §1–§19).

    Decision order: an explicit active boundary never enters through the
    passive path; hard non-passive violations (Re < -uncertainty, |R|
    beyond bound+uncertainty, causality violation, conjugate-symmetry
    violation) fail closed; small violations within declared uncertainty
    report ``passivity_unresolved_with_uncertainty`` — never clipped to a
    PASS. A finite measured band alone can never claim global causality:
    measured evidence without a parametric or stable-rational causal
    basis reports ``causality_unresolved_finite_band``. Unstable fitted
    poles produce ``unstable_fit`` regardless of how good the
    frequency-domain residual looked (#705 §9, ABI50). FD↔TD residual or
    energy-growth failures produce ``time_domain_realization_mismatch``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    _require_finite(
        passive_reflection_bound, 'passive_reflection_bound'
    )
    reasons: list[str] = []
    error_components: list[BoundaryErrorComponent] = []

    # --- passivity component (#705 §3–§5) -------------------------------
    resistive_uncertainty = (
        evidence.uncertainty.resistive_uncertainty
        if evidence.uncertainty is not None
        else None
    )
    reflection_uncertainty = (
        evidence.uncertainty.reflection_uncertainty
        if evidence.uncertainty is not None
        else None
    )
    resistive_violation_depth = 0.0
    if evidence.min_resistive_value is not None and (
        evidence.min_resistive_value < 0
    ):
        resistive_violation_depth = -evidence.min_resistive_value
        error_components.append('measurement_impedance')
    reflection_violation_depth = 0.0
    if evidence.max_reflection_magnitude is not None and (
        evidence.max_reflection_magnitude > passive_reflection_bound
    ):
        reflection_violation_depth = (
            evidence.max_reflection_magnitude - passive_reflection_bound
        )

    hard_resistive_violation = (
        resistive_violation_depth > (resistive_uncertainty or 0.0)
    )
    hard_reflection_violation = (
        reflection_violation_depth > (reflection_uncertainty or 0.0)
    )
    soft_violation = (
        (resistive_violation_depth > 0 and not hard_resistive_violation)
        or (
            reflection_violation_depth > 0
            and not hard_reflection_violation
        )
    )

    if evidence.passivity_class == 'nonpassive_unexpected':
        hard_resistive_violation = True
        reasons.append(
            'the evidence is declared nonpassive_unexpected — a passive '
            'material path cannot admit it'
        )

    # --- causality component (#705 §6–§7) -------------------------------
    if evidence.causality_check == 'inconsistent':
        causality_state: BoundaryCausalityState = (
            'causality_violation_detected'
        )
    elif evidence.boundary_class == 'parametric_physical_model':
        causality_state = 'causal_by_physical_parametric_model'
    elif (
        rational_fit is not None
        and rational_fit.pole_stability(pole_stability_margin)
        == 'stable_realization'
    ):
        causality_state = 'causal_by_stable_rational_realization'
    elif evidence.boundary_class in (
        'measured_frequency_domain_impedance',
        'measured_complex_reflection',
        'inverse_fitted_physical_model',
        'rational_frequency_domain_fit',
    ):
        causality_state = 'causality_unresolved_finite_band'
    else:
        causality_state = 'unknown'
    if causality_state == 'causality_unresolved_finite_band':
        reasons.append(
            'a finite measured band cannot prove global causality — '
            'Kramers–Kronig-style checks stay within the declared band '
            '(#705 §6)'
        )
        error_components.append('out_of_band_assumption')

    # --- stability component (#705 §9/§13–§15) --------------------------
    if rational_fit is not None:
        stability_state = rational_fit.pole_stability(
            pole_stability_margin
        )
        if rational_fit.passivity_enforcement != 'none':
            error_components.append('fit_model')
        if rational_fit.repair_delta is not None:
            error_components.append('passivity_repair_delta')
    elif td_realization is not None:
        stability_state = 'stability_unknown'
    elif evidence.boundary_class in (
        'parametric_physical_model',
        'measured_frequency_domain_impedance',
        'measured_complex_reflection',
        'active_control_boundary',
    ):
        stability_state = 'stability_unknown'
    else:
        stability_state = 'stability_unknown'

    # --- solver band vs evidence/fit band (#705 §12) --------------------
    solver_band_within: bool | None = None
    evidence_band = (
        evidence.measured_band_low_hz,
        evidence.measured_band_high_hz,
    )
    fit_band = (
        (rational_fit.fit_band_low_hz, rational_fit.fit_band_high_hz)
        if rational_fit is not None
        else (None, None)
    )
    if solver_band_low_hz is not None and solver_band_high_hz is not None:
        solver_band_within = True
        for band, label in (
            (fit_band, 'fit band'),
            (evidence_band, 'evidence band'),
        ):
            blo, bhi = band
            if blo is None or bhi is None:
                continue
            if solver_band_low_hz < blo or solver_band_high_hz > bhi:
                solver_band_within = False
                reasons.append(
                    f'the requested solver band extends beyond the {label} '
                    '— out-of-band behavior is undeclared (#705 §12)'
                )
                error_components.append('out_of_band_assumption')
                break

    # --- TD residual / energy checks (#705 §14/§15) ---------------------
    td_mismatch = False
    if td_realization is not None:
        error_components.append('td_realization_error')
        error_components.append('solver_discretization')
        if (
            td_realization.residual_tolerance is not None
            and td_realization.fd_magnitude_residual_db is not None
            and td_realization.fd_magnitude_residual_db
            > td_realization.residual_tolerance
        ):
            td_mismatch = True
            reasons.append(
                'the converted TD realization exceeds its declared '
                'magnitude residual tolerance — a plausible FD fit is '
                'not a verified TD realization (#705 §14)'
            )
        if td_realization.energy_growth_observed:
            td_mismatch = True
            reasons.append(
                'the long-time energy fixture shows unphysical growth '
                '— solver/boundary realization failure (#705 §15)'
            )
        if (
            td_realization.boundary_stability_margin is not None
            and td_realization.boundary_stability_margin < 0
        ):
            td_mismatch = True
            reasons.append(
                'the boundary update violates its solver-specific '
                'stability margin — separate from the free-field CFL '
                'check (Toyoda 2018, #705 §13)'
            )

    # --- conjugate symmetry / reality (#705 §7) -------------------------
    symmetry_violated = (
        evidence.conjugate_symmetry == 'violated'
        or (
            td_realization is not None
            and td_realization.conjugate_symmetry == 'violated'
        )
    )
    if symmetry_violated:
        reasons.append(
            'the frequency-domain representation violates the reality/'
            'conjugate-symmetry condition — it cannot map to a real '
            'time-domain boundary (#705 §7)'
        )

    # --- aggregate verdict ----------------------------------------------
    if evidence.boundary_class == 'active_control_boundary':
        state: BoundaryRealizabilityState = 'active_boundary_explicit'
        passivity_class: BoundaryPassivityClass = (
            'active_boundary_explicit'
        )
        reasons.append(
            'an intentional active boundary requires its own '
            'control/stability authority — never the passive-material '
            'path (#705 §4, #533)'
        )
    elif (
        hard_resistive_violation
        or hard_reflection_violation
        or causality_state == 'causality_violation_detected'
    ):
        state = 'nonpassive_input'
        passivity_class = 'nonpassive_unexpected'
        if (
            hard_resistive_violation
            and resistive_violation_depth > 0
        ):
            reasons.append(
                f'negative resistive part {evidence.min_resistive_value} '
                'exceeds the declared uncertainty — NONPASSIVE_INPUT, '
                'never clipped to zero (#705 §5)'
            )
        if hard_reflection_violation:
            reasons.append(
                'reflection magnitude exceeds the passive bound beyond '
                'its declared uncertainty'
            )
        if causality_state == 'causality_violation_detected':
            reasons.append(
                'a causality violation was detected in the declared '
                'check band'
            )
    elif stability_state == 'unstable_realization':
        state = 'unstable_fit'
        passivity_class = (
            evidence.passivity_class
            if evidence.passivity_class != 'unknown'
            else 'passive_boundary'
        )
        reasons.append(
            'the rational realization has poles outside the stable '
            'domain — a good-looking FD residual is not a TD-safe '
            'boundary (#705 §9)'
        )
    elif td_mismatch or symmetry_violated:
        state = 'time_domain_realization_mismatch'
        passivity_class = (
            evidence.passivity_class
            if evidence.passivity_class != 'unknown'
            else 'passive_boundary'
        )
    elif soft_violation:
        state = 'passivity_unresolved_with_uncertainty'
        passivity_class = evidence.passivity_class
        reasons.append(
            'apparent small passivity violation within declared '
            'measurement uncertainty — review, never silent clipping '
            '(#705 §5)'
        )
    elif evidence.boundary_class == 'unknown' or (
        evidence.min_resistive_value is None
        and evidence.max_reflection_magnitude is None
        and evidence.boundary_class != 'parametric_physical_model'
    ):
        state = 'insufficient_evidence'
        passivity_class = evidence.passivity_class
        reasons.append(
            'no quantitative passivity evidence was declared'
        )
    else:
        passivity_class = (
            evidence.passivity_class
            if evidence.passivity_class != 'unknown'
            else 'passive_boundary'
        )
        limited = (
            solver_band_within is False
            or evidence.interpolation
            in ('independent_component_splines', 'declared_external')
            or stability_state == 'marginally_stable_review'
            or evidence.extrapolation
            in ('constant_endpoint', 'arbitrary_endpoint')
            or causality_state == 'causality_supported_with_limitations'
        )
        if evidence.interpolation in (
            'independent_component_splines', 'declared_external'
        ):
            reasons.append(
                'interpolation/smoothing is part of boundary identity — '
                'independent component splines can fabricate |R|>1 or '
                'non-causal phase (#705 §11)'
            )
        if stability_state == 'marginally_stable_review':
            reasons.append(
                'poles lie inside the stability margin — review required'
            )
        if evidence.extrapolation in (
            'constant_endpoint', 'arbitrary_endpoint'
        ):
            reasons.append(
                'endpoint extrapolation can create nonphysical DC/'
                'high-frequency behavior (#705 §12)'
            )
            error_components.append('out_of_band_assumption')
        if causality_state == 'causality_unresolved_finite_band':
            state = 'causality_unresolved_finite_band'
        elif td_realization is not None and not td_mismatch:
            state = 'stable_numerical_realization'
            reasons.append(
                'TD realization verified against FD evidence within the '
                'declared residual envelope'
            )
        elif not limited and causality_state in (
            'causal_by_physical_parametric_model',
            'causal_by_stable_rational_realization',
        ):
            state = 'passive_causal_validated'
        else:
            state = 'passive_with_limitations'

    dedup_components = tuple(dict.fromkeys(error_components))
    payload = dict(
        document_id=document_id,
        evidence_ref=boundary_evidence_binding(evidence),
        rational_fit_ref=(
            boundary_rational_fit_binding(rational_fit)
            if rational_fit is not None
            else None
        ),
        td_realization_ref=(
            td_realization_binding(td_realization)
            if td_realization is not None
            else None
        ),
        solver_band_low_hz=solver_band_low_hz,
        solver_band_high_hz=solver_band_high_hz,
        state=state,
        passivity_class=passivity_class,
        causality_state=causality_state,
        stability_state=stability_state,
        solver_band_within_evidence=solver_band_within,
        error_components=dedup_components,
        reasons=tuple(reasons),
        evaluation_version=REALIZ_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadBoundaryRealizabilityAssessment, payload,
        'assessment_id', 'assessment_sha256', 'bdass',
    )


__all__ = [
    'BoundaryCausalityCheck',
    'BoundaryCausalityState',
    'BoundaryErrorComponent',
    'BoundaryEvidenceClass',
    'BoundaryExtrapolationKind',
    'BoundaryInterpolationKind',
    'BoundaryPassivityClass',
    'BoundaryQuantityConvention',
    'BoundaryRealizabilityState',
    'BoundaryStabilityState',
    'CadBoundaryEvidenceRecord',
    'CadBoundaryRationalFit',
    'CadBoundaryRealizabilityAssessment',
    'CadBoundaryUncertaintyBreakdown',
    'CadRationalPole',
    'CadTdImpedanceRealization',
    'ConjugateSymmetryState',
    'REALIZ_AUTHORITY_SCHEMA_VERSION',
    'REALIZ_EVALUATION_VERSION',
    'RationalFitForm',
    'RationalFitVariable',
    'RationalPassivityEnforcement',
    'RationalStablePoleConstraint',
    'RealizationTimeDomain',
    'TdSolverFamily',
    'boundary_evidence_binding',
    'boundary_rational_fit_binding',
    'build_boundary_evidence_record',
    'build_boundary_rational_fit',
    'build_td_impedance_realization',
    'evaluate_boundary_realizability',
    'td_realization_binding',
]
