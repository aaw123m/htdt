"""Wave ↔ geometrical hybrid-handoff qualification authority
(#687, REV58-NUMERIC).

A hybrid prediction is honest only when the handoff between its branches
is declared: which wave result hands which band to which GA result, with
what normalization, on which crossover filters, and with which treatment
of phase, overlap and double counting. A crossover corner picked for a
flat magnitude plot is not an acoustic transition frequency. A GA branch
without phase authority cannot be added to a wave pressure IR into a
full-band phase claim. Energy that appears in both branches inside the
overlap is counted twice unless the decomposition declared otherwise.
This module is the fail-closed layer that evaluates whether a declared
handoff is *qualified*, *gapped*, *double-counted* or simply
unverifiable — it composes the per-branch fidelity authorities (#683
wave, #685 geometric) rather than re-deriving them.

Scope discipline (#687):

- Component identity is content-addressed — change any input (wave
  prediction, GA prediction, scene revision, source/receiver) and the
  hybrid is a new profile, not an edit (#687 §1).
- The transition band is *declared*, never assumed — there is no
  universal Schroeder constant in this authority (#673 owns the
  Schroeder question; this authority records what was used and why).
- Pressure-domain and energy-domain branches can only be composed under
  an explicit normalization model — never silently summed (#687 §3/§4).
- Overlap, gap and double-count are structural verdicts derived from the
  declared component bands and the phenomenon declarations — not
  narrative judgment (#687 §8/§9).
- Observable-level validity is separate from the handoff verdict: a
  handoff can be overlap-qualified while still unqualified for complex
  phase or auralization (#687 §10/§19).
- Calibration tuned on the validation holdout cannot claim independent
  validation (#687 §14).

Records:

- :class:`HybridCompositionProfile` — the sealed declaration: component
  pins, normalization, time alignment, transition band, crossover
  filter, output capability, phenomenon declarations and composition
  axes; optionally bound to an existing R160
  ``NumericalHybridCompositionSpec`` by ref.
- :class:`HybridTransitionQualification` +
  :func:`evaluate_hybrid_handoff` — the sealed fail-closed verdict:
  ``overlap_qualified`` / ``qualified_with_limitations`` /
  ``gap_in_capability`` / ``double_count_risk`` /
  ``transition_unqualified`` / ``insufficient_evidence``.

Literature basis
----------------
- Yokota, Toyoda & Sakamoto (2021), "Room-acoustic field reproduction
  using FDTD and image/back-tracing methods", Acoustical Science and
  Technology — DOI 10.1250/ast.42.170. Basis for crossover-filter and
  phase-continuity declarations.
- Wittebol, Wang, Hornikx & Vorländer (2024), "Hybrid room-acoustic
  simulation combining time-domain discontinuous Galerkin with image
  sources and a diffusion model", Applied Acoustics — DOI
  10.1016/j.apacoust.2024.110068. Basis for the early/late + frequency
  hybridization axes and the normalization pinning.
- Yang & Mak (2021), "The transition frequency and its relationship to
  wave/geometric acoustics in rooms", Building Services Engineering —
  DOI 10.1177/0143624421994229. Basis for transition-band selection
  basis declarations.
- Treble documentation — common free-field reference normalization
  (low-pass wave + high-pass GA + sum): basis for
  ``common_free_field_reference`` normalization declarations.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


HYBRID_HANDOFF_SCHEMA_VERSION = 'hybrid-handoff-1'
HYBRID_HANDOFF_EVALUATION_VERSION = 'hybrid-handoff-eval-1'

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

HybridTransitionKind = Literal[
    'hard_split',
    'filtered_crossover',
    'overlap_blend',
    'frequency_dependent_confidence_blend',
    'custom_validated_composition',
]
"""How the two branches are joined across the transition band
(#687 §2)."""

HybridizationAxis = Literal[
    'frequency_domain', 'time_domain', 'path_domain', 'energy_domain'
]
"""The axis along which composition occurs — frequency-domain crossover
is the common case, but early/late (time) and path-domain compositions
exist too; they are declared, not conflated (#687 §16)."""

BranchDomainSemantics = Literal[
    'complex_pressure',
    'energy_histogram',
    'band_energy',
    'stochastic_ir_synthesis',
    'mixed_declared',
]
"""What each branch's result *means* numerically — complex pressure
(phase-bearing) vs energy histogram (phase-less) vs stochastic IR are
not freely mixable (#687 §4)."""

HybridOutputCapability = Literal[
    'complex_phase_bearing',
    'magnitude_energy_only',
    'synthesized_phase',
    'mixed_capability_by_band',
]
"""What the composed output is allowed to claim — a GA branch without
phase authority means the hybrid never claims full-band complex phase
(#687 §4/§10)."""

HybridPhenomenon = Literal[
    'direct_path',
    'specular_reflection',
    'diffraction',
    'scattering_late',
    'modal_response',
    'interference',
    'other_declared',
]
"""Physical phenomena a branch may carry; a phenomenon declared in both
branches inside the overlap band is a double-count risk (#687 §7/§8)."""

HandoffState = Literal[
    'overlap_qualified',
    'qualified_with_limitations',
    'gap_in_capability',
    'double_count_risk',
    'transition_unqualified',
    'insufficient_evidence',
]
"""The structural verdict on the transition (#687 §8/§9)."""

ContinuityMetricState = Literal[
    'continuous', 'discontinuous', 'not_evaluated'
]
"""Per-metric continuity state (#687 §11) — reported separately, never
folded into a score."""

HybridFixtureId = Literal[
    'HYB10', 'HYB20', 'HYB30', 'HYB40', 'HYB50', 'HYB60', 'HYB70',
    'HYB80', 'HYB90',
]
"""#687 §13 fixtures:

- HYB10 analytic hard-split reference,
- HYB20 filtered-crossover magnitude continuity,
- HYB30 phase discontinuity detection,
- HYB40 direct-arrival timing through the transition,
- HYB50 double-count detection (specular in both branches),
- HYB60 energy gap detection,
- HYB70 spatial continuity,
- HYB80 broadband reference comparison,
- HYB90 full transition sweep.
"""

HybridObservable = Literal[
    'magnitude_fr',
    'complex_phase',
    'modal_response',
    'early_reflection_timing',
    'late_decay',
    'spatial_field',
    'auralization',
    'other_declared',
]


# ----------------------------------------------------------------------
# Component & composition declarations


class HybridComponentPin(BaseModel):
    """One branch of the composition, pinned to its prediction and its
    per-branch fidelity profile (#687 §1).

    ``qualified_band_hz`` declares the band the branch is qualified
    *for* on the consumed observable — the handoff evaluator uses it to
    derive overlap vs gap. When unset, the branch's valid band is
    unknown and the handoff cannot be qualified.
    """

    model_config = ConfigDict(frozen=True)

    prediction_ref: AuthorityRef
    fidelity_profile_ref: AuthorityRef | None = None
    qualified_band_hz: tuple[float, float] | None = None
    semantics: BranchDomainSemantics
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'HybridComponentPin':
        if self.prediction_ref.ref_sha256 is None:
            raise ValueError(
                'component prediction_ref must pin its sha256'
            )
        if self.fidelity_profile_ref is not None:
            if self.fidelity_profile_ref.ref_sha256 is None:
                raise ValueError(
                    'fidelity_profile_ref must pin its sha256'
                )
            if self.fidelity_profile_ref.kind not in (
                'wave_fidelity_profile',
                'geometric_fidelity_profile',
            ):
                raise ValueError(
                    'fidelity_profile_ref must pin a '
                    "'wave_fidelity_profile' or "
                    "'geometric_fidelity_profile'"
                )
        if self.qualified_band_hz is not None:
            low, high = self.qualified_band_hz
            _require_finite(low, 'component band low')
            _require_finite(high, 'component band high')
            if low >= high:
                raise ValueError(
                    'qualified_band_hz must be low < high'
                )
        return self


class SourceNormalizationSpec(BaseModel):
    """How the two branches are brought onto a common scale (#687 §3).

    A wave branch and a GA branch can carry pressure and energy
    respectively — they can only be composed under a declared
    normalization model, never silently summed.
    """

    model_config = ConfigDict(frozen=True)

    source_strength_basis: str = Field(min_length=1)
    """What the source quantity is — ``unit_volume_velocity_m3_s`` /
    ``unit_power_w`` / declared other."""
    reference_condition: str = Field(min_length=1)
    """The shared reference — ``common_free_field_reference`` /
    ``anechoic_1m`` / declared other."""
    quantity: Literal['pressure', 'energy', 'power', 'mixed_declared']
    directivity_basis: str | None = None
    phase_reference: str | None = None
    spl_state: Literal['absolute', 'relative', 'unknown'] = 'unknown'
    normalization_authority_ref: AuthorityRef | None = None
    """Pins the existing R160 normalization authority record when the
    composition reuses one."""

    @model_validator(mode='after')
    def _check(self) -> 'SourceNormalizationSpec':
        if self.normalization_authority_ref is not None and (
            self.normalization_authority_ref.ref_sha256 is None
        ):
            raise ValueError(
                'normalization_authority_ref must pin its sha256'
            )
        return self


class TimeAlignmentSpec(BaseModel):
    """How the two branches' time axes are reconciled (#687 §5)."""

    model_config = ConfigDict(frozen=True)

    common_t0: str = Field(min_length=1)
    """What both branches call t=0 — ``source_emission`` /
    ``direct_path_arrival`` / declared other."""
    direct_path_delay_handling: str = Field(min_length=1)
    solver_latency_refs: str | None = None
    resampling: str | None = None
    fractional_delay: str | None = None
    filter_group_delay_policy: Literal[
        'zero_phase',
        'group_delay_compensated',
        'uncompensated',
        'unknown',
    ] = 'unknown'


class CrossoverFilterSpec(BaseModel):
    """The crossover filter authority (#687 §6).

    The filter is declared as an acoustic design choice — family, order,
    corners, phase characteristic — never just 'the thing that made the
    magnitude flat'. Complementarity must be declared when intended.
    """

    model_config = ConfigDict(frozen=True)

    family: str = Field(min_length=1)
    """``linkwitz_riley`` / ``butterworth`` / ``fir_linear_phase`` /
    declared other."""
    order: int | None = Field(default=None, ge=1)
    corner_hz: tuple[float, float] | None = None
    phase_characteristic: str | None = None
    zero_phase: bool | None = None
    windowing: str | None = None
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    complementary_declared: bool | None = None

    @model_validator(mode='after')
    def _check(self) -> 'CrossoverFilterSpec':
        if self.corner_hz is not None:
            low, high = self.corner_hz
            _require_finite(low, 'corner low')
            _require_finite(high, 'corner high')
            if low >= high:
                raise ValueError('corner_hz must be low < high')
        if self.sample_rate_hz is not None:
            _require_finite(self.sample_rate_hz, 'sample_rate_hz')
        return self


class TransitionBandSpec(BaseModel):
    """The declared transition band (#687 §2).

    ``selection_basis`` records *why* this band was chosen —
    ``component_envelope_intersection`` /
    ``schroeder_informed_but_not_fixed`` / ``fixture_derived`` /
    ``declared_other`` — a transition must always carry a basis, never
    an implicit universal constant.
    """

    model_config = ConfigDict(frozen=True)

    kind: HybridTransitionKind
    low_edge_hz: float = Field(gt=0.0)
    nominal_hz: float | None = Field(default=None, gt=0.0)
    high_edge_hz: float = Field(gt=0.0)
    blend_law: str = Field(min_length=1)
    """How weight shifts across the band — ``linear_frequency`` /
    ``raised_cosine`` / ``confidence_weighted`` / declared other."""
    selection_basis: str = Field(min_length=1)
    schroeder_estimate_hz: float | None = Field(default=None, gt=0.0)
    """Informational only — the estimated Schroeder frequency for
    context, never a prescription."""

    @model_validator(mode='after')
    def _check(self) -> 'TransitionBandSpec':
        _require_finite(self.low_edge_hz, 'transition low edge')
        _require_finite(self.high_edge_hz, 'transition high edge')
        if self.low_edge_hz >= self.high_edge_hz:
            raise ValueError('transition low edge must be < high edge')
        if self.nominal_hz is not None:
            _require_finite(self.nominal_hz, 'transition nominal')
            if not (
                self.low_edge_hz
                <= self.nominal_hz
                <= self.high_edge_hz
            ):
                raise ValueError(
                    'transition nominal must lie inside the band'
                )
        if self.schroeder_estimate_hz is not None:
            _require_finite(
                self.schroeder_estimate_hz, 'schroeder_estimate_hz'
            )
        return self


class PhenomenonDeclaration(BaseModel):
    """The mechanisms a branch carries inside the overlap (#687 §7).

    A phenomenon declared in *both* branches within the overlap band is
    a double-count risk unless an explicit decomposition disclaimer
    exists — the evaluator derives that verdict from these two
    declarations.
    """

    model_config = ConfigDict(frozen=True)

    branch: Literal['wave', 'geometric']
    phenomena: tuple[HybridPhenomenon, ...] = Field(min_length=1)
    decomposition_note: str | None = None
    """Declared when a phenomenon is present in both branches *by
    design* — e.g. wave diffraction below, GA diffraction above, with a
    clean spectral split."""

    @model_validator(mode='after')
    def _check(self) -> 'PhenomenonDeclaration':
        if len(set(self.phenomena)) != len(self.phenomena):
            raise ValueError('phenomena must be unique per branch')
        return self


class ContinuityEvidence(BaseModel):
    """Measured transition continuity (#687 §11).

    Magnitude, phase, group-delay, ringing, direct-arrival timing,
    reflection timing, EDC and band-energy conservation are reported
    separately — they are different physical questions.
    """

    model_config = ConfigDict(frozen=True)

    magnitude_step_db: float | None = Field(default=None, ge=0.0)
    magnitude_ripple_db: float | None = Field(default=None, ge=0.0)
    phase_discontinuity_rad: float | None = Field(default=None, ge=0.0)
    group_delay_artifact_s: float | None = Field(default=None, ge=0.0)
    impulse_ringing: str | None = None
    direct_arrival_continuity: ContinuityMetricState = 'not_evaluated'
    reflection_timing_continuity: ContinuityMetricState = 'not_evaluated'
    edc_continuity: ContinuityMetricState = 'not_evaluated'
    band_energy_conservation: ContinuityMetricState = 'not_evaluated'
    spatial_continuity: ContinuityMetricState = 'not_evaluated'
    fixture_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'ContinuityEvidence':
        for label in (
            'magnitude_step_db',
            'magnitude_ripple_db',
            'phase_discontinuity_rad',
            'group_delay_artifact_s',
        ):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, f'continuity {label}')
        for ref in self.fixture_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'continuity fixture_refs must pin their sha256'
                )
        return self


class ObservableValidity(BaseModel):
    """Per-observable validity of the composed result (#687 §10)."""

    model_config = ConfigDict(frozen=True)

    observable: HybridObservable | str = Field(min_length=1)
    valid_band_hz: tuple[float, float] | None = None
    state: Literal[
        'qualified', 'limited', 'unqualified', 'not_evaluated'
    ]
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ObservableValidity':
        if self.valid_band_hz is not None:
            low, high = self.valid_band_hz
            _require_finite(low, 'observable band low')
            _require_finite(high, 'observable band high')
            if low >= high:
                raise ValueError(
                    'observable valid_band_hz must be low < high'
                )
        return self


class CalibrationDiscipline(BaseModel):
    """Whether the handoff was tuned against the validation set
    (#687 §14).

    A crossover or normalization tuned on the holdout can never claim
    independent validation — the qualification caps its state when this
    flag is set.
    """

    model_config = ConfigDict(frozen=True)

    tuned_parameters: tuple[str, ...] = ()
    bounds_objective: str | None = None
    holdout_refs: tuple[AuthorityRef, ...] = ()
    tuned_on_validation_holdout: bool = False

    @model_validator(mode='after')
    def _check(self) -> 'CalibrationDiscipline':
        if len(set(self.tuned_parameters)) != len(self.tuned_parameters):
            raise ValueError('tuned_parameters must be unique')
        for ref in self.holdout_refs:
            if ref.ref_sha256 is None:
                raise ValueError('holdout_refs must pin their sha256')
        return self


class MetricProvenance(BaseModel):
    """Which branch or composition produced a reported metric
    (#687 §18)."""

    model_config = ConfigDict(frozen=True)

    metric: str = Field(min_length=1)
    computed_from: Literal[
        'wave_branch',
        'ga_branch',
        'hybrid_waveform',
        'hybrid_energy_histogram',
        'mixed_band_aggregation',
    ]


# ----------------------------------------------------------------------
# Sealed records


class HybridCompositionProfile(BaseModel):
    """The sealed declaration of one wave↔GA composition (#687).

    Component identity is content-addressed: the profile sha256 changes
    whenever the wave prediction, GA prediction, scene revision,
    normalization, transition band, filter or any declared phenomenon
    changes.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    wave_component: HybridComponentPin
    ga_component: HybridComponentPin
    scene_revision_ref: AuthorityRef | None = None
    source_ref: AuthorityRef | None = None
    receiver_ref: AuthorityRef | None = None
    source_normalization: SourceNormalizationSpec
    time_alignment: TimeAlignmentSpec
    transition: TransitionBandSpec
    crossover_filter: CrossoverFilterSpec | None = None
    output_capability: HybridOutputCapability
    phenomenon_declarations: tuple[PhenomenonDeclaration, ...] = Field(
        min_length=2, max_length=2
    )
    composition_axes: tuple[HybridizationAxis, ...] = Field(
        min_length=1
    )
    composition_spec_ref: AuthorityRef | None = None
    """Pins the R160 ``NumericalHybridCompositionSpec`` when the
    composition reuses it."""
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=HYBRID_HANDOFF_SCHEMA_VERSION, min_length=1
    )
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'wave_component': self.wave_component.model_dump(mode='json'),
            'ga_component': self.ga_component.model_dump(mode='json'),
            'scene_revision_ref': (
                self.scene_revision_ref.model_dump(mode='json')
                if self.scene_revision_ref is not None
                else None
            ),
            'source_ref': (
                self.source_ref.model_dump(mode='json')
                if self.source_ref is not None
                else None
            ),
            'receiver_ref': (
                self.receiver_ref.model_dump(mode='json')
                if self.receiver_ref is not None
                else None
            ),
            'source_normalization': (
                self.source_normalization.model_dump(mode='json')
            ),
            'time_alignment': self.time_alignment.model_dump(mode='json'),
            'transition': self.transition.model_dump(mode='json'),
            'crossover_filter': (
                self.crossover_filter.model_dump(mode='json')
                if self.crossover_filter is not None
                else None
            ),
            'output_capability': self.output_capability,
            'phenomenon_declarations': [
                entry.model_dump(mode='json')
                for entry in self.phenomenon_declarations
            ],
            'composition_axes': list(self.composition_axes),
            'composition_spec_ref': (
                self.composition_spec_ref.model_dump(mode='json')
                if self.composition_spec_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'HybridCompositionProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        for ref, label in (
            (self.scene_revision_ref, 'scene_revision_ref'),
            (self.source_ref, 'source_ref'),
            (self.receiver_ref, 'receiver_ref'),
            (self.composition_spec_ref, 'composition_spec_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.composition_spec_ref is not None and (
            self.composition_spec_ref.kind
            != 'numerical_hybrid_composition_spec'
        ):
            raise ValueError(
                "composition_spec_ref must pin a "
                "'numerical_hybrid_composition_spec'"
            )
        branches = {
            entry.branch for entry in self.phenomenon_declarations
        }
        if branches != {'wave', 'geometric'}:
            raise ValueError(
                'phenomenon_declarations must cover exactly the wave '
                'and geometric branches'
            )
        if len(set(self.composition_axes)) != len(self.composition_axes):
            raise ValueError('composition_axes must be unique')
        filter_required = self.transition.kind in (
            'filtered_crossover',
            'overlap_blend',
            'frequency_dependent_confidence_blend',
        )
        if filter_required and self.crossover_filter is None:
            raise ValueError(
                f"transition kind {self.transition.kind!r} requires a "
                'crossover filter declaration'
            )
        # Pressure-domain + energy-domain branches can only be composed
        # under an explicit normalization model (#687 §3/§4).
        wave_sem = self.wave_component.semantics
        ga_sem = self.ga_component.semantics
        pressure_like = {'complex_pressure'}
        energy_like = {'energy_histogram', 'band_energy'}
        if (
            (wave_sem in pressure_like and ga_sem in energy_like)
            or (wave_sem in energy_like and ga_sem in pressure_like)
        ):
            if (
                self.source_normalization.normalization_authority_ref
                is None
                and self.source_normalization.quantity
                != 'mixed_declared'
            ):
                raise ValueError(
                    'composing pressure-domain and energy-domain '
                    'branches requires an explicit normalization '
                    'authority or quantity="mixed_declared"'
                )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('hybrid composition profile hash mismatch')
        if self.profile_id != _semantic_id('hybprof', expected):
            raise ValueError(
                'hybrid composition profile id does not match its hash'
            )
        return self


def hybrid_profile_binding(
    profile: HybridCompositionProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hybrid_composition_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class HybridTransitionQualification(BaseModel):
    """The sealed fail-closed verdict on a hybrid handoff (#687 §8–§14).

    ``handoff_state`` is the structural verdict;
    ``observable_validity`` carries per-observable qualifications that
    can be narrower than the overall state (a handoff can be
    overlap-qualified but still unqualified for complex phase).
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    handoff_state: HandoffState
    gap_band_hz: tuple[float, float] | None = None
    double_count_findings: tuple[str, ...] = ()
    observable_validity: tuple[ObservableValidity, ...] = ()
    continuity: ContinuityEvidence | None = None
    calibration: CalibrationDiscipline | None = None
    metric_provenances: tuple[MetricProvenance, ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=HYBRID_HANDOFF_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'handoff_state': self.handoff_state,
            'gap_band_hz': list(self.gap_band_hz)
            if self.gap_band_hz is not None
            else None,
            'double_count_findings': list(self.double_count_findings),
            'observable_validity': [
                entry.model_dump(mode='json')
                for entry in self.observable_validity
            ],
            'continuity': (
                self.continuity.model_dump(mode='json')
                if self.continuity is not None
                else None
            ),
            'calibration': (
                self.calibration.model_dump(mode='json')
                if self.calibration is not None
                else None
            ),
            'metric_provenances': [
                entry.model_dump(mode='json')
                for entry in self.metric_provenances
            ],
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'HybridTransitionQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.kind != 'hybrid_composition_profile':
            raise ValueError(
                "profile_ref must pin a 'hybrid_composition_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.gap_band_hz is not None:
            low, high = self.gap_band_hz
            _require_finite(low, 'gap band low')
            _require_finite(high, 'gap band high')
            if low >= high:
                raise ValueError('gap_band_hz must be low < high')
        if self.handoff_state == 'gap_in_capability' and (
            self.gap_band_hz is None
        ):
            raise ValueError(
                'a gap verdict must state the gap band'
            )
        if self.handoff_state == 'double_count_risk' and not (
            self.double_count_findings
        ):
            raise ValueError(
                'a double-count verdict must name the findings'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'hybrid transition qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('hybqual', expected):
            raise ValueError(
                'hybrid qualification id does not match its hash'
            )
        return self


def hybrid_qualification_binding(
    qualification: HybridTransitionQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hybrid_transition_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ----------------------------------------------------------------------
# Evaluation


def evaluate_hybrid_handoff(
    document_id: str,
    profile: HybridCompositionProfile,
    *,
    continuity: ContinuityEvidence | None = None,
    calibration: CalibrationDiscipline | None = None,
    observable_validity: Sequence[ObservableValidity] = (),
    metric_provenances: Sequence[MetricProvenance] = (),
    evaluated_at_utc: str | None = None,
) -> HybridTransitionQualification:
    """Fail-closed verdict on a hybrid handoff (#687 §8/§9/§11/§14).

    - Both component ``qualified_band_hz`` declared → compare bands:
      wave_hi < ga_lo → ``gap_in_capability`` (with the gap band);
      overlap → check phenomenon declarations inside the overlap.
    - A phenomenon declared in *both* branches → ``double_count_risk``
      (unless an explicit ``decomposition_note`` declares the split).
    - Overlap exists and no double count → qualified only when
      continuity evidence is declared and no discontinuous metric was
      recorded.
    - ``tuned_on_validation_holdout`` caps the verdict at
      ``qualified_with_limitations``.
    - Unknown bands → ``insufficient_evidence``; a filtered kind whose
      group-delay policy is uncompensated gets a reason, not a silent
      pass.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []
    double_count_findings: list[str] = []
    gap_band: tuple[float, float] | None = None

    wave_band = profile.wave_component.qualified_band_hz
    ga_band = profile.ga_component.qualified_band_hz

    wave_phenomena = next(
        (
            entry
            for entry in profile.phenomenon_declarations
            if entry.branch == 'wave'
        ),
        None,
    )
    ga_phenomena = next(
        (
            entry
            for entry in profile.phenomenon_declarations
            if entry.branch == 'geometric'
        ),
        None,
    )
    shared_phenomena: list[str] = []
    if wave_phenomena is not None and ga_phenomena is not None:
        shared = set(wave_phenomena.phenomena) & set(
            ga_phenomena.phenomena
        )
        decomposition_noted = bool(
            wave_phenomena.decomposition_note
            and ga_phenomena.decomposition_note
        )
        for phenomenon in sorted(shared):
            if decomposition_noted:
                limitations.append(
                    f'{phenomenon} present in both branches under a '
                    'declared decomposition'
                )
            else:
                double_count_findings.append(
                    f'{phenomenon} declared in both branches inside '
                    'the overlap band'
                )

    # --- structural verdict -----------------------------------------------
    if wave_band is None or ga_band is None:
        handoff_state: HandoffState = 'insufficient_evidence'
        reasons.append(
            'one or both component qualified bands are undeclared'
        )
    else:
        wave_low, wave_high = wave_band
        ga_low, ga_high = ga_band
        if wave_high < ga_low:
            handoff_state = 'gap_in_capability'
            gap_band = (wave_high, ga_low)
            reasons.append(
                f'wave branch ends at {wave_high} Hz but GA branch '
                f'starts at {ga_low} Hz'
            )
        elif ga_high < wave_low:
            handoff_state = 'gap_in_capability'
            gap_band = (ga_high, wave_low)
            reasons.append(
                'branch bands are disjoint (GA below wave)'
            )
        else:
            # Bands overlap or meet.
            if double_count_findings:
                handoff_state = 'double_count_risk'
                reasons.append(
                    'unresolved phenomena present in both branches'
                )
            else:
                # Continuity evidence decides qualified vs limited.
                if continuity is None:
                    handoff_state = 'transition_unqualified'
                    reasons.append(
                        'no transition continuity evidence was declared'
                    )
                else:
                    discontinuous = [
                        label
                        for label in (
                            'direct_arrival_continuity',
                            'reflection_timing_continuity',
                            'edc_continuity',
                            'band_energy_conservation',
                            'spatial_continuity',
                        )
                        if getattr(continuity, label) == 'discontinuous'
                    ]
                    if discontinuous:
                        handoff_state = 'double_count_risk' if (
                            double_count_findings
                        ) else 'transition_unqualified'
                        reasons.append(
                            'discontinuous metrics: '
                            + ', '.join(discontinuous)
                        )
                    elif all(
                        getattr(continuity, label) == 'continuous'
                        for label in (
                            'direct_arrival_continuity',
                            'reflection_timing_continuity',
                            'edc_continuity',
                            'band_energy_conservation',
                        )
                    ):
                        handoff_state = 'overlap_qualified'
                    else:
                        handoff_state = 'qualified_with_limitations'
                        limitations.append(
                            'continuity partially evaluated — some '
                            'metrics not_evaluated'
                        )

    # --- calibration discipline ---------------------------------------------
    if calibration is not None and (
        calibration.tuned_on_validation_holdout
    ):
        limitations.append(
            'transition was tuned on the validation holdout — '
            'independent validation cannot be claimed'
        )
        if handoff_state == 'overlap_qualified':
            handoff_state = 'qualified_with_limitations'

    # --- filter policy ---------------------------------------------------------
    if (
        profile.transition.kind in ('filtered_crossover', 'overlap_blend')
        and profile.time_alignment.filter_group_delay_policy
        in ('uncompensated', 'unknown')
    ):
        limitations.append(
            'crossover group delay is not compensated'
        )
        if handoff_state == 'overlap_qualified':
            handoff_state = 'qualified_with_limitations'

    # --- output capability sanity ----------------------------------------------
    ga_sem = profile.ga_component.semantics
    wave_sem = profile.wave_component.semantics
    if (
        profile.output_capability == 'complex_phase_bearing'
        and ga_sem in ('energy_histogram', 'band_energy')
        and wave_sem != 'complex_pressure'
    ):
        limitations.append(
            'complex-phase output claimed but neither branch carries '
            'phase authority'
        )
        if handoff_state == 'overlap_qualified':
            handoff_state = 'qualified_with_limitations'

    return _seal(
        HybridTransitionQualification,
        {
            'document_id': document_id,
            'profile_ref': hybrid_profile_binding(profile),
            'handoff_state': handoff_state,
            'gap_band_hz': list(gap_band) if gap_band is not None else None,
            'double_count_findings': sorted(set(double_count_findings)),
            'observable_validity': [
                entry.model_dump(mode='json')
                for entry in observable_validity
            ],
            'continuity': (
                continuity.model_dump(mode='json')
                if continuity is not None
                else None
            ),
            'calibration': (
                calibration.model_dump(mode='json')
                if calibration is not None
                else None
            ),
            'metric_provenances': [
                entry.model_dump(mode='json')
                for entry in metric_provenances
            ],
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': HYBRID_HANDOFF_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'hybqual',
    )


# ----------------------------------------------------------------------
# Builders


def build_hybrid_composition_profile(
    document_id: str,
    wave_component: HybridComponentPin,
    ga_component: HybridComponentPin,
    source_normalization: SourceNormalizationSpec,
    time_alignment: TimeAlignmentSpec,
    transition: TransitionBandSpec,
    output_capability: HybridOutputCapability,
    phenomenon_declarations: Sequence[PhenomenonDeclaration],
    composition_axes: Sequence[HybridizationAxis],
    *,
    scene_revision_ref: AuthorityRef | None = None,
    source_ref: AuthorityRef | None = None,
    receiver_ref: AuthorityRef | None = None,
    crossover_filter: CrossoverFilterSpec | None = None,
    composition_spec_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> HybridCompositionProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        HybridCompositionProfile,
        {
            'document_id': document_id,
            'wave_component': wave_component.model_dump(mode='json'),
            'ga_component': ga_component.model_dump(mode='json'),
            'scene_revision_ref': (
                scene_revision_ref.model_dump(mode='json')
                if scene_revision_ref is not None
                else None
            ),
            'source_ref': (
                source_ref.model_dump(mode='json')
                if source_ref is not None
                else None
            ),
            'receiver_ref': (
                receiver_ref.model_dump(mode='json')
                if receiver_ref is not None
                else None
            ),
            'source_normalization': source_normalization.model_dump(
                mode='json'
            ),
            'time_alignment': time_alignment.model_dump(mode='json'),
            'transition': transition.model_dump(mode='json'),
            'crossover_filter': (
                crossover_filter.model_dump(mode='json')
                if crossover_filter is not None
                else None
            ),
            'output_capability': output_capability,
            'phenomenon_declarations': [
                entry.model_dump(mode='json')
                for entry in phenomenon_declarations
            ],
            'composition_axes': list(composition_axes),
            'composition_spec_ref': (
                composition_spec_ref.model_dump(mode='json')
                if composition_spec_ref is not None
                else None
            ),
            'authority_version': HYBRID_HANDOFF_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'hybprof',
    )
