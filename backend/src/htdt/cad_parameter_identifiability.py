"""Calibration parameter identifiability authority (#689, REV58-IDENT).

A calibrated room model can fit measurements very well while the fitted
physical parameters remain non-identifiable, strongly correlated,
prior-dominated or physically non-unique. ``optimizer returned one
vector`` is never ``the room parameters were identified`` — this module
makes that distinction a sealed, fail-closed authority underneath #564's
bounded calibration and beside #604's uncertainty propagation.

- :class:`CadCalibrationParameter` — one sealed calibration parameter:
  its physical/nuisance *role* (§1 taxonomy), *provenance class*
  (measured / lab / prior / calibration-adjusted / inverse-estimated /
  posterior-profile / not-identifiable — §2), hard bounds with distance
  state, prior/regularization declarations and the observable domain
  that actually constrained it (band + source/receiver pins — a
  parameter fitted from one MLP is not automatically room-global).
- :class:`CadSensitivityEvidence` — sealed sensitivity/Jacobian evidence
  (§4): method identity (finite difference / autodiff / Morris / Sobol
  declared / Fisher / profile / multi-start), parameter scaling,
  observable normalization, perturbation method, the frequency and
  source/receiver domain it was evaluated on, per-parameter normalized
  sensitivities and collinearity entries. A local Jacobian never claims
  global uniqueness.
- :class:`CadParameterCorrelationEvidence` — sealed correlation /
  trade-off evidence (§5): pair/group dependencies with method identity
  and mandatory assumptions; a marginal interval never hides a long
  correlated ridge.
- :class:`CadEquivalentSolutionSet` — a retained set of practically
  equivalent parameter vectors (§6): members within a declared objective
  tolerance or multimodal posterior support, flagged when they make
  materially different physical claims. Equifinality is never hidden
  behind a single best-fit row.
- :class:`CadIdentifiabilityAssessment` +
  :func:`evaluate_identifiability` — the sealed fail-closed verdict.
  The identifiability *class* (§3: structurally non-unique / weakly
  identifiable / practically identifiable within data / prior-dominated
  / bound-dominated / model-discrepancy-limited / insufficient evidence)
  and the *parameter claim* (§16: identified within domain / weakly
  identified / not identifiable / model-dependent) are derived
  separately from the caller-declared *predictive* claim — a calibrated
  predictive twin can coexist with unidentified physics. Weakly
  identified parameters emit typed evidence needs (§18).

Composition:

- #564 ``cad_model_calibration`` — owns the bounded calibration
  workflow, preregistered spec and holdout discipline; this authority
  consumes its results (``calibration_run_ref`` pins) and answers the
  separate question *were the fitted parameters themselves identified*.
- #566 — model-form/benchmark evidence composed via the
  ``model_discrepancy`` input: a physically implausible model family
  caps the parameter claim even when the fit likelihood is good.
- #577/#604 — decision relevance: parameter ambiguity matters only when
  equivalent sets change the engineering ranking; the assessment keeps
  the ambiguity visible either way.
- #675 optimizer qualification — optimizer trapping (multi-start
  variance) stays separable from inverse-problem non-uniqueness.

Literature basis
----------------
- Mondet et al., *From absorption to impedance: Enhancing boundary
  conditions in room acoustic simulations*, Applied Acoustics 157
  (2020) 106884 — recovering complex impedance from real-valued
  absorption is non-unique; constraints/priors stabilize but remain
  prior information, never measurement identification.
- Pilch, *Optimization-based method for the calibration of geometrical
  acoustic models*, Applied Acoustics 170 (2020) 107495 — calibration
  adjusts multiple uncertain inputs and validates on a different source
  position; predictive holdout success is not parameter identification.
- Wulbusch et al., *Bayesian Parameter Identification in Impedance
  Boundary Conditions for Helmholtz Problems*, SIAM J. Sci. Comput.
  (2023) — under a misspecified boundary model some bands still yield
  high-likelihood parameters: posterior confidence inside a wrong model
  is not physical truth (``model_discrepancy_limited``).
- Wang & Rathsam, *The influence of absorption factors on the
  sensitivity of a virtual room's sound field to scattering
  coefficients*, Applied Acoustics 69(12) (2008) — parameter
  observability is experiment/room dependent; identifiability state is
  domain-bound, never a property of the parameter name.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


IDENT_AUTHORITY_SCHEMA_VERSION = 'ident-param-1'
IDENT_EVALUATION_VERSION = 'ident-eval-1'

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


# ---------------------------------------------------------------------------
# Taxonomies (#689)
# ---------------------------------------------------------------------------

CalibrationParameterRole = Literal[
    'geometry',
    'source_position',
    'receiver_position',
    'source_level',
    'source_directivity',
    'boundary_absorption',
    'boundary_complex_impedance',
    'scattering',
    'porous_material_model',
    'environment',
    'device_dsp',
    'nuisance_registration',
    'model_discrepancy',
]
"""#689 §1 — every calibration parameter names its physical role; one
generic vector of unnamed optimizer variables is never accepted."""

ParameterProvenance = Literal[
    'directly_measured',
    'manufacturer_lab_evidence',
    'prior_assumed',
    'calibration_adjusted',
    'inverse_estimated',
    'posterior_profile_estimate',
    'not_identifiable',
]
"""#689 §2 — measured, assumed, calibrated and inverse-estimated
quantities stay distinct; a calibrated coefficient never overwrites the
source laboratory/material evidence."""

IdentifiabilityClass = Literal[
    'structurally_non_unique',
    'weakly_identifiable',
    'practically_identifiable_within_data',
    'prior_dominated',
    'bound_dominated',
    'model_discrepancy_limited',
    'insufficient_evidence',
]
"""#689 §3 — structural (the model/observable mapping permits equivalent
solutions) vs practical (limited/noisy data) stay distinct; the labels
never claim a formal mathematical proof the method did not provide."""

SensitivityMethod = Literal[
    'finite_difference',
    'autodiff',
    'morris_screening',
    'sobol_declared',
    'fisher_information',
    'profile_objective',
    'multi_start_ensemble',
    'other_declared',
]

CorrelationMethod = Literal[
    'covariance_posterior',
    'fisher_hessian_local',
    'profile_objective',
    'posterior_samples',
    'multi_start_equivalent',
    'global_sensitivity',
    'other_declared',
]

CorrelationClass = Literal['strong', 'moderate', 'weak', 'unknown']

ParameterClaim = Literal[
    'parameter_identified_within_domain',
    'parameter_weakly_identified',
    'parameter_not_identifiable',
    'parameter_model_dependent',
]
"""#689 §16 parameter-side claims only — the predictive claim is a
separate caller-declared field so the two verdicts never merge."""

PredictiveClaim = Literal[
    'predictive_model_calibrated',
    'predictive_model_validated_on_holdout',
    'not_evaluated',
]

ModelDiscrepancyState = Literal[
    'confirmed', 'plausible', 'none_declared', 'not_evaluated',
]
"""#689 §8 — whether the chosen model family can represent the observed
phenomenon; ``plausible``/``confirmed`` caps the parameter claim."""

BoundDistanceState = Literal[
    'interior',
    'at_lower_bound',
    'at_upper_bound',
    'unbounded',
    'undeclared',
]

EvidenceNeed = Literal[
    'alternate_source_position',
    'diagnostic_receiver_position',
    'additional_frequency_band',
    'impedance_or_material_measurement',
    'geometry_survey',
    'source_directivity_evidence',
    'independent_modal_or_decay_measurement',
    'absolute_level_calibration',
    'other_declared',
]
"""#689 §18 — the typed evidence needs a weakly identified parameter can
generate. This issue owns the *diagnosis*, not the measurement planner."""

CollinearityClass = Literal['collinear', 'nearly_collinear', 'independent', 'unknown']


# ---------------------------------------------------------------------------
# Embedded blocks
# ---------------------------------------------------------------------------


class CadIdentFrequencyDomain(BaseModel):
    """The band an observation constrained the parameter on (#689 §12).

    A parameter may be identifiable only in selected bands; the domain
    travels with the evidence so an inferred value is never extrapolated
    outside the band that constrained it.
    """

    model_config = ConfigDict(frozen=True)

    low_hz: float | None = None
    high_hz: float | None = None
    label: str | None = None

    @model_validator(mode='after')
    def valid_domain(self) -> 'CadIdentFrequencyDomain':
        for tag, value in (
            ('low_hz', self.low_hz), ('high_hz', self.high_hz)
        ):
            if value is not None:
                _require_finite(value, f'frequency domain {tag}')
        if (
            self.low_hz is not None
            and self.high_hz is not None
            and self.high_hz < self.low_hz
        ):
            raise ValueError('frequency domain must be ascending')
        return self


class CadSensitivityEntry(BaseModel):
    """One parameter's normalized local sensitivity (#689 §4)."""

    model_config = ConfigDict(frozen=True)

    parameter_label: str = Field(min_length=1)
    normalized_sensitivity: float | None = None
    insensitive: bool = False
    observable_label: str | None = None

    @model_validator(mode='after')
    def valid_entry(self) -> 'CadSensitivityEntry':
        if self.normalized_sensitivity is not None:
            _require_finite(
                self.normalized_sensitivity, 'normalized_sensitivity'
            )
        return self


class CadCollinearityEntry(BaseModel):
    """A declared nearly-collinear parameter pair (#689 §4)."""

    model_config = ConfigDict(frozen=True)

    parameter_a: str = Field(min_length=1)
    parameter_b: str = Field(min_length=1)
    collinearity: CollinearityClass
    detail: str | None = None


class CadCorrelationPair(BaseModel):
    """One parameter-pair dependency (#689 §5)."""

    model_config = ConfigDict(frozen=True)

    parameter_a: str = Field(min_length=1)
    parameter_b: str = Field(min_length=1)
    correlation: float | None = None
    correlation_class: CorrelationClass = 'unknown'
    detail: str | None = None

    @model_validator(mode='after')
    def valid_pair(self) -> 'CadCorrelationPair':
        if self.correlation is not None:
            _require_finite(self.correlation, 'correlation')
            if not -1.0 <= self.correlation <= 1.0:
                raise ValueError('correlation must lie in [-1, 1]')
        return self


class CadEquivalentSolution(BaseModel):
    """One member of a retained near-equivalent parameter set (#689 §6)."""

    model_config = ConfigDict(frozen=True)

    member_label: str = Field(min_length=1)
    parameter_values_json: str = '{}'
    objective_delta: float = 0.0
    physically_distinct_claims: bool = False
    claim_summary: str | None = None

    @model_validator(mode='after')
    def valid_member(self) -> 'CadEquivalentSolution':
        _require_finite(self.objective_delta, 'objective_delta')
        if self.objective_delta < 0:
            raise ValueError('objective_delta is measured from the best')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadCalibrationParameter(BaseModel):
    """One sealed calibration parameter record (#689 §1/§2/§7).

    Role, provenance class, fitted/nominal value, bounds and distance
    state, prior/regularization declarations and the observable domain
    that constrained it. ``inverse_estimated``/``calibration_adjusted``
    values pin the calibration run that produced them; a
    ``not_identifiable`` parameter may legitimately carry no value.
    """

    model_config = ConfigDict(frozen=True)

    parameter_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    parameter_label: str = Field(min_length=1)
    role: CalibrationParameterRole
    provenance: ParameterProvenance
    unit: str = Field(min_length=1)
    fitted_value: float | None = None
    bound_min: float | None = None
    bound_max: float | None = None
    bound_distance: BoundDistanceState = 'undeclared'
    prior_evidence_ref: AuthorityRef | None = None
    prior_strength: Literal['informative', 'weak', 'none', 'undeclared'] = (
        'undeclared'
    )
    regularization: Literal[
        'none', 'declared_penalty', 'regularized', 'undeclared'
    ] = 'undeclared'
    calibration_run_ref: AuthorityRef | None = None
    effective_domain: CadIdentFrequencyDomain | None = None
    constraining_source_refs: tuple[AuthorityRef, ...] = ()
    constraining_receiver_refs: tuple[AuthorityRef, ...] = ()
    constraining_observable_refs: tuple[AuthorityRef, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    parameter_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_parameter(self) -> 'CadCalibrationParameter':
        _require_iso8601(self.declared_at_utc, 'parameter declared_at_utc')
        for label, value in (
            ('fitted_value', self.fitted_value),
            ('bound_min', self.bound_min),
            ('bound_max', self.bound_max),
        ):
            if value is not None:
                _require_finite(value, label)
        if (
            self.bound_min is not None
            and self.bound_max is not None
            and self.bound_max < self.bound_min
        ):
            raise ValueError('bounds must be ascending')
        if self.provenance in (
            'calibration_adjusted',
            'inverse_estimated',
            'posterior_profile_estimate',
        ) and self.calibration_run_ref is None:
            raise ValueError(
                f'a {self.provenance} parameter must pin the '
                'calibration run that produced it — an adjusted value '
                'without provenance is indistinguishable from a '
                'measured one'
            )
        if self.provenance in (
            'directly_measured', 'manufacturer_lab_evidence'
        ) and self.prior_evidence_ref is None:
            raise ValueError(
                f'a {self.provenance} parameter must pin its source '
                'evidence — measured and calibrated quantities never '
                'share one store'
            )
        for label, ref in (
            ('prior_evidence_ref', self.prior_evidence_ref),
            ('calibration_run_ref', self.calibration_run_ref),
            *[
                (f'constraining_source_refs[{i}]', r)
                for i, r in enumerate(self.constraining_source_refs)
            ],
            *[
                (f'constraining_receiver_refs[{i}]', r)
                for i, r in enumerate(self.constraining_receiver_refs)
            ],
            *[
                (f'constraining_observable_refs[{i}]', r)
                for i, r in enumerate(self.constraining_observable_refs)
            ],
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        expected = _hash(self.identity_payload())
        if self.parameter_sha256 != expected:
            raise ValueError('calibration parameter hash mismatch')
        if self.parameter_id != _semantic_id('calprm', expected):
            raise ValueError(
                'calibration parameter id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'parameter_label': self.parameter_label,
            'role': self.role,
            'provenance': self.provenance,
            'unit': self.unit,
            'fitted_value': self.fitted_value,
            'bound_min': self.bound_min,
            'bound_max': self.bound_max,
            'bound_distance': self.bound_distance,
            'prior_evidence_ref': (
                self.prior_evidence_ref.model_dump(mode='json')
                if self.prior_evidence_ref is not None
                else None
            ),
            'prior_strength': self.prior_strength,
            'regularization': self.regularization,
            'calibration_run_ref': (
                self.calibration_run_ref.model_dump(mode='json')
                if self.calibration_run_ref is not None
                else None
            ),
            'effective_domain': (
                self.effective_domain.model_dump(mode='json')
                if self.effective_domain is not None
                else None
            ),
            'constraining_source_refs': [
                r.model_dump(mode='json')
                for r in self.constraining_source_refs
            ],
            'constraining_receiver_refs': [
                r.model_dump(mode='json')
                for r in self.constraining_receiver_refs
            ],
            'constraining_observable_refs': [
                r.model_dump(mode='json')
                for r in self.constraining_observable_refs
            ],
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    @property
    def is_nuisance(self) -> bool:
        """Registration/nuisance parameters (#689 §9) are typed by role —
        they must never silently trade off against physical material
        parameters."""
        return self.role == 'nuisance_registration'


def calibration_parameter_binding(
    parameter: CadCalibrationParameter,
) -> AuthorityRef:
    return AuthorityRef(
        kind='calibration_parameter',
        ref_id=parameter.parameter_id,
        ref_sha256=parameter.parameter_sha256,
    )


class CadSensitivityEvidence(BaseModel):
    """Sealed local/global sensitivity evidence (#689 §4).

    Method identity, parameter scaling, observable normalization and
    perturbation details are mandatory — a sensitivity number without
    its normalization is not reproducible. ``entries`` carry
    per-parameter normalized sensitivities and ``collinearity`` keeps
    nearly-collinear effects visible; a local Jacobian never proves
    global uniqueness.
    """

    model_config = ConfigDict(frozen=True)

    sensitivity_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    calibration_run_ref: AuthorityRef | None = None
    method: SensitivityMethod
    method_detail: str | None = None
    parameter_scaling: str = Field(min_length=1)
    observable_normalization: str = Field(min_length=1)
    perturbation_size: float | None = None
    perturbation_method: str | None = None
    frequency_domain: CadIdentFrequencyDomain | None = None
    source_refs: tuple[AuthorityRef, ...] = ()
    receiver_refs: tuple[AuthorityRef, ...] = ()
    observable_refs: tuple[AuthorityRef, ...] = ()
    entries: tuple[CadSensitivityEntry, ...] = ()
    collinearity: tuple[CadCollinearityEntry, ...] = ()
    algorithm_version: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    sensitivity_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_sensitivity(self) -> 'CadSensitivityEvidence':
        _require_iso8601(
            self.declared_at_utc, 'sensitivity declared_at_utc'
        )
        if self.method in ('finite_difference', 'autodiff') and (
            self.perturbation_size is None
            and self.perturbation_method is None
        ):
            raise ValueError(
                'a local perturbative method must declare its '
                'perturbation size or method — the sensitivity depends '
                'on it'
            )
        if self.perturbation_size is not None:
            _require_finite(
                self.perturbation_size, 'perturbation_size'
            )
            if self.perturbation_size <= 0:
                raise ValueError('perturbation size must be positive')
        if not self.entries:
            raise ValueError(
                'sensitivity evidence requires at least one parameter '
                'entry'
            )
        for label, ref in (
            ('calibration_run_ref', self.calibration_run_ref),
            *[
                (f'source_refs[{i}]', r)
                for i, r in enumerate(self.source_refs)
            ],
            *[
                (f'receiver_refs[{i}]', r)
                for i, r in enumerate(self.receiver_refs)
            ],
            *[
                (f'observable_refs[{i}]', r)
                for i, r in enumerate(self.observable_refs)
            ],
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        expected = _hash(self.identity_payload())
        if self.sensitivity_sha256 != expected:
            raise ValueError('sensitivity evidence hash mismatch')
        if self.sensitivity_id != _semantic_id('idsens', expected):
            raise ValueError(
                'sensitivity evidence id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'calibration_run_ref': (
                self.calibration_run_ref.model_dump(mode='json')
                if self.calibration_run_ref is not None
                else None
            ),
            'method': self.method,
            'method_detail': self.method_detail,
            'parameter_scaling': self.parameter_scaling,
            'observable_normalization': self.observable_normalization,
            'perturbation_size': self.perturbation_size,
            'perturbation_method': self.perturbation_method,
            'frequency_domain': (
                self.frequency_domain.model_dump(mode='json')
                if self.frequency_domain is not None
                else None
            ),
            'source_refs': [
                r.model_dump(mode='json') for r in self.source_refs
            ],
            'receiver_refs': [
                r.model_dump(mode='json') for r in self.receiver_refs
            ],
            'observable_refs': [
                r.model_dump(mode='json') for r in self.observable_refs
            ],
            'entries': [e.model_dump(mode='json') for e in self.entries],
            'collinearity': [
                c.model_dump(mode='json') for c in self.collinearity
            ],
            'algorithm_version': self.algorithm_version,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    def entry_for(
        self, parameter_label: str
    ) -> CadSensitivityEntry | None:
        for entry in self.entries:
            if entry.parameter_label == parameter_label:
                return entry
        return None

    def collinear_partners(self, parameter_label: str) -> tuple[str, ...]:
        partners: list[str] = []
        for entry in self.collinearity:
            if entry.collinearity in ('collinear', 'nearly_collinear'):
                if entry.parameter_a == parameter_label:
                    partners.append(entry.parameter_b)
                elif entry.parameter_b == parameter_label:
                    partners.append(entry.parameter_a)
        return tuple(partners)


def sensitivity_evidence_binding(
    evidence: CadSensitivityEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='ident_sensitivity_evidence',
        ref_id=evidence.sensitivity_id,
        ref_sha256=evidence.sensitivity_sha256,
    )


class CadParameterCorrelationEvidence(BaseModel):
    """Sealed parameter correlation / trade-off evidence (#689 §5).

    Pair/group dependencies with the method identity and mandatory
    assumptions. No single universal threshold is baked in — the
    qualitative class travels beside any numeric correlation.
    """

    model_config = ConfigDict(frozen=True)

    correlation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    calibration_run_ref: AuthorityRef | None = None
    method: CorrelationMethod
    method_detail: str | None = None
    assumptions: tuple[str, ...] = Field(min_length=1)
    pairs: tuple[CadCorrelationPair, ...] = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    correlation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_correlation(self) -> 'CadParameterCorrelationEvidence':
        _require_iso8601(
            self.declared_at_utc, 'correlation declared_at_utc'
        )
        if self.calibration_run_ref is not None and (
            self.calibration_run_ref.ref_sha256 is None
        ):
            raise ValueError(
                'calibration_run_ref must carry its sha256 pin'
            )
        expected = _hash(self.identity_payload())
        if self.correlation_sha256 != expected:
            raise ValueError('correlation evidence hash mismatch')
        if self.correlation_id != _semantic_id('idcorr', expected):
            raise ValueError(
                'correlation evidence id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'calibration_run_ref': (
                self.calibration_run_ref.model_dump(mode='json')
                if self.calibration_run_ref is not None
                else None
            ),
            'method': self.method,
            'method_detail': self.method_detail,
            'assumptions': list(self.assumptions),
            'pairs': [p.model_dump(mode='json') for p in self.pairs],
            'algorithm_version': self.algorithm_version,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    def strongest_class_for(
        self, parameter_label: str
    ) -> CorrelationClass | None:
        """The strongest declared dependency class involving a parameter."""
        order = ('strong', 'moderate', 'weak', 'unknown')
        best: CorrelationClass | None = None
        for pair in self.pairs:
            if (
                parameter_label
                in (pair.parameter_a, pair.parameter_b)
            ):
                if (
                    best is None
                    or order.index(pair.correlation_class)
                    < order.index(best)
                ):
                    best = pair.correlation_class
        return best

    def partners_of(self, parameter_label: str) -> tuple[str, ...]:
        partners: list[str] = []
        for pair in self.pairs:
            if pair.parameter_a == parameter_label:
                partners.append(pair.parameter_b)
            elif pair.parameter_b == parameter_label:
                partners.append(pair.parameter_a)
        return tuple(partners)


def correlation_evidence_binding(
    evidence: CadParameterCorrelationEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='ident_correlation_evidence',
        ref_id=evidence.correlation_id,
        ref_sha256=evidence.correlation_sha256,
    )


class CadEquivalentSolutionSet(BaseModel):
    """A retained set of practically equivalent parameter vectors
    (#689 §6).

    Calibration keeps multiple near-equivalent parameter sets — objective
    difference within the declared tolerance or multimodal posterior
    support — instead of discarding all but the numerical winner.
    ``physically_distinct_claims`` marks members that would make
    materially different physical claims.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    calibration_run_ref: AuthorityRef | None = None
    tolerance_objective_delta: float
    multimodal: bool = False
    members: tuple[CadEquivalentSolution, ...] = Field(min_length=2)
    retention_method: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_set(self) -> 'CadEquivalentSolutionSet':
        _require_iso8601(self.declared_at_utc, 'set declared_at_utc')
        _require_finite(
            self.tolerance_objective_delta,
            'tolerance_objective_delta',
        )
        if self.tolerance_objective_delta <= 0:
            raise ValueError(
                'the practical equivalence tolerance must be positive '
                '— "equal" without a tolerance is not an equivalence '
                'claim'
            )
        for member in self.members:
            if member.objective_delta > self.tolerance_objective_delta:
                raise ValueError(
                    f'member {member.member_label!r} exceeds the '
                    'declared equivalence tolerance — it is not a member '
                    'of this equivalent set'
                )
        if self.calibration_run_ref is not None and (
            self.calibration_run_ref.ref_sha256 is None
        ):
            raise ValueError(
                'calibration_run_ref must carry its sha256 pin'
            )
        expected = _hash(self.identity_payload())
        if self.set_sha256 != expected:
            raise ValueError('equivalent solution set hash mismatch')
        if self.set_id != _semantic_id('ideqset', expected):
            raise ValueError(
                'equivalent solution set id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'calibration_run_ref': (
                self.calibration_run_ref.model_dump(mode='json')
                if self.calibration_run_ref is not None
                else None
            ),
            'tolerance_objective_delta': self.tolerance_objective_delta,
            'multimodal': self.multimodal,
            'members': [m.model_dump(mode='json') for m in self.members],
            'retention_method': self.retention_method,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @property
    def has_distinct_physical_claims(self) -> bool:
        return any(
            m.physically_distinct_claims for m in self.members
        )


def equivalent_set_binding(
    solution_set: CadEquivalentSolutionSet,
) -> AuthorityRef:
    return AuthorityRef(
        kind='ident_equivalent_set',
        ref_id=solution_set.set_id,
        ref_sha256=solution_set.set_sha256,
    )


class CadIdentifiabilityAssessment(BaseModel):
    """The sealed identifiability verdict for one parameter (#689 §16).

    ``identifiability_class`` (§3) and ``parameter_claim`` (§16) are
    derived fail-closed; ``predictive_claim`` is caller-declared and kept
    a separate axis — a model can be predictively useful while the
    physical parameter remains unidentified. ``evidence_needs`` carries
    the typed §18 diagnosis output; ``domain_note`` keeps the
    source/receiver/frequency restriction visible.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    parameter_ref: AuthorityRef
    identifiability_class: IdentifiabilityClass
    parameter_claim: ParameterClaim
    predictive_claim: PredictiveClaim = 'not_evaluated'
    model_discrepancy: ModelDiscrepancyState = 'not_evaluated'
    sensitivity_ref: AuthorityRef | None = None
    correlation_refs: tuple[AuthorityRef, ...] = ()
    equivalent_set_refs: tuple[AuthorityRef, ...] = ()
    domain_note: str | None = None
    evidence_needs: tuple[EvidenceNeed, ...] = ()
    correlated_with: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'CadIdentifiabilityAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        for label, ref in (
            ('parameter_ref', self.parameter_ref),
            ('sensitivity_ref', self.sensitivity_ref),
            *[
                (f'correlation_refs[{i}]', r)
                for i, r in enumerate(self.correlation_refs)
            ],
            *[
                (f'equivalent_set_refs[{i}]', r)
                for i, r in enumerate(self.equivalent_set_refs)
            ],
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.parameter_claim == 'parameter_identified_within_domain' and (
            self.domain_note is None
        ):
            raise ValueError(
                'an identified-within-domain claim must name the domain '
                '— band/source/receiver restriction travels with the '
                'claim (#689 §10–§12)'
            )
        if self.identifiability_class in (
            'structurally_non_unique',
            'prior_dominated',
            'bound_dominated',
            'model_discrepancy_limited',
            'insufficient_evidence',
        ) and self.parameter_claim == 'parameter_identified_within_domain':
            raise ValueError(
                f'a {self.identifiability_class} parameter cannot carry '
                'an identified-within-domain claim'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('identifiability assessment hash mismatch')
        if self.assessment_id != _semantic_id('idassess', expected):
            raise ValueError(
                'identifiability assessment id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'parameter_ref': self.parameter_ref.model_dump(mode='json'),
            'identifiability_class': self.identifiability_class,
            'parameter_claim': self.parameter_claim,
            'predictive_claim': self.predictive_claim,
            'model_discrepancy': self.model_discrepancy,
            'sensitivity_ref': (
                self.sensitivity_ref.model_dump(mode='json')
                if self.sensitivity_ref is not None
                else None
            ),
            'correlation_refs': [
                r.model_dump(mode='json')
                for r in self.correlation_refs
            ],
            'equivalent_set_refs': [
                r.model_dump(mode='json')
                for r in self.equivalent_set_refs
            ],
            'domain_note': self.domain_note,
            'evidence_needs': list(self.evidence_needs),
            'correlated_with': list(self.correlated_with),
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


def build_calibration_parameter(
    *,
    document_id: str,
    parameter_label: str,
    role: CalibrationParameterRole,
    provenance: ParameterProvenance,
    unit: str,
    fitted_value: float | None = None,
    bound_min: float | None = None,
    bound_max: float | None = None,
    bound_distance: BoundDistanceState = 'undeclared',
    prior_evidence_ref: AuthorityRef | None = None,
    prior_strength: Literal[
        'informative', 'weak', 'none', 'undeclared'
    ] = 'undeclared',
    regularization: Literal[
        'none', 'declared_penalty', 'regularized', 'undeclared'
    ] = 'undeclared',
    calibration_run_ref: AuthorityRef | None = None,
    effective_domain: CadIdentFrequencyDomain | None = None,
    constraining_source_refs: tuple[AuthorityRef, ...] | list[
        AuthorityRef
    ] = (),
    constraining_receiver_refs: tuple[AuthorityRef, ...] | list[
        AuthorityRef
    ] = (),
    constraining_observable_refs: tuple[AuthorityRef, ...] | list[
        AuthorityRef
    ] = (),
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadCalibrationParameter:
    """Seal one calibration parameter."""
    payload = dict(
        document_id=document_id,
        parameter_label=parameter_label,
        role=role,
        provenance=provenance,
        unit=unit,
        fitted_value=fitted_value,
        bound_min=bound_min,
        bound_max=bound_max,
        bound_distance=bound_distance,
        prior_evidence_ref=prior_evidence_ref,
        prior_strength=prior_strength,
        regularization=regularization,
        calibration_run_ref=calibration_run_ref,
        effective_domain=effective_domain,
        constraining_source_refs=tuple(constraining_source_refs),
        constraining_receiver_refs=tuple(constraining_receiver_refs),
        constraining_observable_refs=tuple(constraining_observable_refs),
        authority_version=IDENT_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadCalibrationParameter, payload,
        'parameter_id', 'parameter_sha256', 'calprm',
    )


def build_sensitivity_evidence(
    *,
    document_id: str,
    method: SensitivityMethod,
    parameter_scaling: str,
    observable_normalization: str,
    entries: tuple[CadSensitivityEntry, ...] | list[CadSensitivityEntry],
    calibration_run_ref: AuthorityRef | None = None,
    method_detail: str | None = None,
    perturbation_size: float | None = None,
    perturbation_method: str | None = None,
    frequency_domain: CadIdentFrequencyDomain | None = None,
    source_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    receiver_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    observable_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    collinearity: tuple[CadCollinearityEntry, ...] | list[
        CadCollinearityEntry
    ] = (),
    algorithm_version: str = 'unspecified',
    declared_at_utc: str | None = None,
) -> CadSensitivityEvidence:
    """Seal sensitivity/Jacobian evidence."""
    payload = dict(
        document_id=document_id,
        calibration_run_ref=calibration_run_ref,
        method=method,
        method_detail=method_detail,
        parameter_scaling=parameter_scaling,
        observable_normalization=observable_normalization,
        perturbation_size=perturbation_size,
        perturbation_method=perturbation_method,
        frequency_domain=frequency_domain,
        source_refs=tuple(source_refs),
        receiver_refs=tuple(receiver_refs),
        observable_refs=tuple(observable_refs),
        entries=tuple(entries),
        collinearity=tuple(collinearity),
        algorithm_version=algorithm_version,
        authority_version=IDENT_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadSensitivityEvidence, payload,
        'sensitivity_id', 'sensitivity_sha256', 'idsens',
    )


def build_correlation_evidence(
    *,
    document_id: str,
    method: CorrelationMethod,
    assumptions: tuple[str, ...] | list[str],
    pairs: tuple[CadCorrelationPair, ...] | list[CadCorrelationPair],
    calibration_run_ref: AuthorityRef | None = None,
    method_detail: str | None = None,
    algorithm_version: str = 'unspecified',
    declared_at_utc: str | None = None,
) -> CadParameterCorrelationEvidence:
    """Seal parameter correlation / trade-off evidence."""
    payload = dict(
        document_id=document_id,
        calibration_run_ref=calibration_run_ref,
        method=method,
        method_detail=method_detail,
        assumptions=tuple(assumptions),
        pairs=tuple(pairs),
        algorithm_version=algorithm_version,
        authority_version=IDENT_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadParameterCorrelationEvidence, payload,
        'correlation_id', 'correlation_sha256', 'idcorr',
    )


def build_equivalent_solution_set(
    *,
    document_id: str,
    tolerance_objective_delta: float,
    members: tuple[CadEquivalentSolution, ...] | list[
        CadEquivalentSolution
    ],
    retention_method: str,
    calibration_run_ref: AuthorityRef | None = None,
    multimodal: bool = False,
    declared_at_utc: str | None = None,
) -> CadEquivalentSolutionSet:
    """Seal a retained equivalent-solution set."""
    payload = dict(
        document_id=document_id,
        calibration_run_ref=calibration_run_ref,
        tolerance_objective_delta=tolerance_objective_delta,
        multimodal=multimodal,
        members=tuple(members),
        retention_method=retention_method,
        authority_version=IDENT_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadEquivalentSolutionSet, payload,
        'set_id', 'set_sha256', 'ideqset',
    )


def evaluate_identifiability(
    *,
    document_id: str,
    parameter: CadCalibrationParameter,
    sensitivity: CadSensitivityEvidence | None = None,
    correlations: tuple[CadParameterCorrelationEvidence, ...] | list[
        CadParameterCorrelationEvidence
    ] = (),
    equivalent_sets: tuple[CadEquivalentSolutionSet, ...] | list[
        CadEquivalentSolutionSet
    ] = (),
    model_discrepancy: ModelDiscrepancyState = 'not_evaluated',
    predictive_claim: PredictiveClaim = 'not_evaluated',
    domain_note: str | None = None,
    strong_correlation_threshold: float = 0.9,
    evidence_needs: tuple[EvidenceNeed, ...] | list[EvidenceNeed] = (),
    evaluated_at_utc: str | None = None,
) -> CadIdentifiabilityAssessment:
    """Fail-closed identifiability verdict (#689).

    Derivation order:

    - an equivalent set whose members make physically distinct claims is
      structural non-uniqueness — fit quality cannot separate them;
    - ``confirmed`` model-form discrepancy caps the claim at
      ``parameter_model_dependent`` (Wulbusch: a misspecified model can
      still yield high-likelihood parameters);
    - a prior/bound-driven estimate is not data-identified —
      ``prior_dominated``/``bound_dominated`` fail to
      ``parameter_not_identifiable``;
    - missing sensitivity evidence is ``insufficient_evidence``;
    - an insensitive entry or a strong correlation/trade-off degrades to
      ``weakly_identifiable``;
    - ``plausible`` model discrepancy likewise caps the claim;
    - otherwise ``practically_identifiable_within_data`` with the
      domain restriction mandatory — never a global identification.

    ``not_evaluated`` model discrepancy caps the claim at
    ``parameter_weakly_identified``: a physical-parameter claim requires
    the model-form question to have been asked.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    _require_finite(
        strong_correlation_threshold, 'strong_correlation_threshold'
    )
    if not 0 < strong_correlation_threshold <= 1.0:
        raise ValueError('correlation threshold must lie in (0, 1]')
    reasons: list[str] = []
    correlated_with: set[str] = set()
    needs: list[EvidenceNeed] = list(evidence_needs)

    def _add_need(need: EvidenceNeed) -> None:
        if need not in needs:
            needs.append(need)

    label = parameter.parameter_label

    distinct_equivalents = any(
        s.has_distinct_physical_claims for s in equivalent_sets
    )
    sensitivity_entry = (
        sensitivity.entry_for(label) if sensitivity is not None else None
    )
    if sensitivity is not None:
        correlated_with.update(sensitivity.collinear_partners(label))
    strong_correlation = False
    for evidence in correlations:
        correlated_with.update(evidence.partners_of(label))
        klass = evidence.strongest_class_for(label)
        if klass == 'strong':
            strong_correlation = True
            continue
        for pair in evidence.pairs:
            if (
                label in (pair.parameter_a, pair.parameter_b)
                and pair.correlation is not None
                and abs(pair.correlation) >= strong_correlation_threshold
            ):
                strong_correlation = True

    def _derive_domain_note() -> str:
        """Derive the domain note from declared constraints so the
        within-domain claim always names its domain."""
        parts: list[str] = []
        if parameter.effective_domain is not None:
            dom = parameter.effective_domain
            if dom.low_hz is not None and dom.high_hz is not None:
                parts.append(f'{dom.low_hz:g}–{dom.high_hz:g} Hz')
            elif dom.label:
                parts.append(dom.label)
        if parameter.constraining_source_refs:
            parts.append(
                f'{len(parameter.constraining_source_refs)} '
                'constraining source(s)'
            )
        if parameter.constraining_receiver_refs:
            parts.append(
                f'{len(parameter.constraining_receiver_refs)} '
                'constraining receiver(s)'
            )
        return (
            '; '.join(parts)
            if parts
            else 'the declared calibration experiment domain'
        )

    if distinct_equivalents:
        identifiability_class = 'structurally_non_unique'
        parameter_claim: ParameterClaim = 'parameter_not_identifiable'
        reasons.append(
            'a retained equivalent-solution set contains physically '
            'distinct parameterizations that fit within the declared '
            'tolerance — the data do not select this parameter value'
        )
        _add_need('alternate_source_position')
        _add_need('additional_frequency_band')
    elif model_discrepancy == 'confirmed':
        identifiability_class = 'model_discrepancy_limited'
        parameter_claim = 'parameter_model_dependent'
        reasons.append(
            'the model family is confirmed unable to represent the '
            'observed phenomenon — fitted parameters compensate for '
            'missing physics and are model-dependent'
        )
        _add_need('impedance_or_material_measurement')
    elif parameter.provenance in (
        'directly_measured', 'manufacturer_lab_evidence'
    ):
        # Evidence-class parameters are not inverse-identified at all;
        # the claim axis stays honest about that.
        identifiability_class = 'practically_identifiable_within_data'
        parameter_claim = 'parameter_identified_within_domain'
        reasons.append(
            'the value comes from direct measurement or laboratory '
            'evidence rather than inverse fitting'
        )
        if domain_note is None:
            domain_note = _derive_domain_note()
    elif parameter.prior_strength == 'informative' or (
        parameter.provenance == 'prior_assumed'
    ):
        identifiability_class = 'prior_dominated'
        parameter_claim = 'parameter_not_identifiable'
        reasons.append(
            'the estimate is dominated by prior information — the prior '
            'stabilized the inverse problem but is not measurement '
            'identification (Mondet et al.)'
        )
        _add_need('impedance_or_material_measurement')
        _add_need('independent_modal_or_decay_measurement')
    elif parameter.bound_distance in (
        'at_lower_bound', 'at_upper_bound'
    ):
        identifiability_class = 'bound_dominated'
        parameter_claim = 'parameter_not_identifiable'
        reasons.append(
            f'the estimate rests {parameter.bound_distance.replace("_", " ")} '
            '— the constraint, not the data, determined the value'
        )
        _add_need('additional_frequency_band')
    elif sensitivity is None and not correlations and not equivalent_sets:
        identifiability_class = 'insufficient_evidence'
        parameter_claim = 'parameter_not_identifiable'
        reasons.append(
            'no sensitivity, correlation or equivalent-solution evidence '
            'was supplied — a good fit alone never identifies a '
            'parameter'
        )
        _add_need('independent_modal_or_decay_measurement')
    elif sensitivity_entry is not None and (
        sensitivity_entry.insensitive
        or (
            sensitivity_entry.normalized_sensitivity is not None
            and abs(sensitivity_entry.normalized_sensitivity) < 1e-9
        )
    ):
        identifiability_class = 'weakly_identifiable'
        parameter_claim = 'parameter_weakly_identified'
        reasons.append(
            'the observables are insensitive to this parameter over the '
            'evaluated domain — the fitted value is barely data-driven'
        )
        _add_need('additional_frequency_band')
        _add_need('diagnostic_receiver_position')
    elif strong_correlation or (
        sensitivity is not None
        and sensitivity.collinear_partners(label)
    ):
        identifiability_class = 'weakly_identifiable'
        parameter_claim = 'parameter_weakly_identified'
        reasons.append(
            'strong parameter correlation/collinearity — the value '
            'trades off against '
            + ', '.join(sorted(correlated_with))
        )
        _add_need('alternate_source_position')
        _add_need('diagnostic_receiver_position')
    elif model_discrepancy == 'plausible':
        identifiability_class = 'model_discrepancy_limited'
        parameter_claim = 'parameter_model_dependent'
        reasons.append(
            'a model-form discrepancy is plausible — the parameter may '
            'be absorbing unrepresented physics'
        )
        _add_need('impedance_or_material_measurement')
    elif model_discrepancy == 'not_evaluated':
        identifiability_class = 'practically_identifiable_within_data'
        parameter_claim = 'parameter_weakly_identified'
        reasons.append(
            'model-form discrepancy was not evaluated — a physical '
            'parameter claim requires that question to have been asked '
            '(#689 §8)'
        )
    else:
        identifiability_class = 'practically_identifiable_within_data'
        parameter_claim = 'parameter_identified_within_domain'
        if domain_note is None:
            domain_note = _derive_domain_note()

    if parameter.role == 'nuisance_registration' and correlated_with:
        reasons.append(
            'nuisance/registration parameter correlates with physical '
            'parameters — a timing/position mismatch can masquerade as '
            'material (#689 §9)'
        )

    payload = dict(
        document_id=document_id,
        parameter_ref=calibration_parameter_binding(parameter),
        identifiability_class=identifiability_class,
        parameter_claim=parameter_claim,
        predictive_claim=predictive_claim,
        model_discrepancy=model_discrepancy,
        sensitivity_ref=(
            sensitivity_evidence_binding(sensitivity)
            if sensitivity is not None
            else None
        ),
        correlation_refs=tuple(
            correlation_evidence_binding(c) for c in correlations
        ),
        equivalent_set_refs=tuple(
            equivalent_set_binding(s) for s in equivalent_sets
        ),
        domain_note=domain_note,
        evidence_needs=tuple(needs),
        correlated_with=tuple(sorted(correlated_with)),
        reasons=tuple(reasons),
        evaluation_version=IDENT_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadIdentifiabilityAssessment, payload,
        'assessment_id', 'assessment_sha256', 'idassess',
    )


__all__ = [
    'BoundDistanceState',
    'CadCalibrationParameter',
    'CadCollinearityEntry',
    'CadCorrelationPair',
    'CadEquivalentSolution',
    'CadEquivalentSolutionSet',
    'CadIdentFrequencyDomain',
    'CadIdentifiabilityAssessment',
    'CadParameterCorrelationEvidence',
    'CadSensitivityEntry',
    'CadSensitivityEvidence',
    'CalibrationParameterRole',
    'CollinearityClass',
    'CorrelationClass',
    'CorrelationMethod',
    'EvidenceNeed',
    'IDENT_AUTHORITY_SCHEMA_VERSION',
    'IDENT_EVALUATION_VERSION',
    'IdentifiabilityClass',
    'ModelDiscrepancyState',
    'ParameterClaim',
    'ParameterProvenance',
    'PredictiveClaim',
    'SensitivityMethod',
    'build_calibration_parameter',
    'build_correlation_evidence',
    'build_equivalent_solution_set',
    'build_sensitivity_evidence',
    'calibration_parameter_binding',
    'correlation_evidence_binding',
    'equivalent_set_binding',
    'evaluate_identifiability',
    'sensitivity_evidence_binding',
]
