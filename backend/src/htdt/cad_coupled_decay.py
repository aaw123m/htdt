"""Coupled-room multi-slope decay authority (#671, REV58-VALIDMETH).

Connected acoustic regions (composing #1029
``ConnectedAcousticRegion``/``AcousticPortalCoupling``) must not be
collapsed into one room RT/T30 when the physical decay is multi-slope:
energy exchange between sub-spaces produces curvature, double-slope or
non-exponential decay in at least one region. A coupled acoustic scene
is therefore *declared* — member regions, portals and door/opening
state (composes #573) are pinned — and multi-slope behaviour is
evaluated from declared evidence, never inferred from a good-looking
single-exponential fit.

Scope discipline (#671):

- Region/portal identity is first-class: which ``AcousticRegion`` set
  and which portals participate, including open/closed/variable state
  (#671 §2; composes #573 portal state, #570/#615 materials).
- Raw decay evidence stays canonical: per-region/position RIR/EDC
  artifacts pin their processing decisions (#676) — noise-floor and
  truncation uncertainty are preserved, never refitted away (#671 §5).
- The single-slope adequacy gate is explicit:
  ``single_slope_adequate`` requires declared evidence the decay is
  not curved and the dynamic range is sufficient; otherwise the state
  is ``multi_slope_supported``/``multi_slope_suspected``/
  ``insufficient_range``/``noise_limited``/``indeterminate`` (#671 §4).
- Multi-slope fits name their model class (double-slope,
  coupled-exponential, non-exponential); early/late components are
  distinct terms, not merged into one T30 (#671 §6).
- Position dependence is real: a decay valid at one receiver is not
  the "room decay" — spatial variability and exchange paths are
  retained (#671 §7).
- What the solver can represent is declared: a solver without
  inter-region exchange cannot evidence a single-slope claim (#671
  §8).

Records:

- :class:`CoupledDecayProfile` — the sealed coupled-scene identity:
  member regions, portals and solver capability.
- :class:`MultiSlopeDecayFit` — one declared multi-component fit bound
  to canonical raw evidence and #676 processing refs.
- :class:`SingleSlopeAdequacyAssessment` — the fail-closed gate verdict
  from :func:`evaluate_single_slope_adequacy`.
- :class:`InterRegionEnergyDecayQualification` — the sealed
  inter-region qualification from
  :func:`evaluate_coupled_decay`: whether a coupled, multi-slope or
  single-slope claim is defensible, with the non-collapse guarantee.

Literature basis
----------------
- Ermann & Johnson (2005), "A new method for the analysis of sound
  decay in coupled rooms", Inter-Noise 2005. Multi-slope decay in
  coupled spaces and its measurable decomposition.
- Pu, Qiu & Wang (2011), JASA 129(4) — DOI 10.1121/1.3553223.
  Bayesian-decay analysis of coupled-room multi-slope evidence;
  single-slope collapse hides components.
- Xiang, Jing & Bockman (2009), JASA 126(2) — DOI 10.1121/1.3168507.
  Coupled-room decay analysis; multi-slope identification in
  architectural acoustics.
- Luizard, Katz & others (2015) — DOI 10.1121/1.4904515.
  Non-exponential / coupled-volume decay measured and modelled
  position-dependently.
- Čurović et al. (2021), Stockwell-transform modal analysis — coupled
  low-frequency decay is modal-density-dependent; see #706 companion
  authority.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


COUPLED_DECAY_SCHEMA_VERSION = 'coupled-decay-1'
COUPLED_DECAY_EVALUATION_VERSION = 'coupled-decay-eval-1'

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


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

DecayBehaviorState = Literal[
    'single_exponential_decay',
    'coupled_volume_double_slope',
    'coupled_volume_multi_slope',
    'low_frequency_modal_multi_slope',
    'mixed_ambiguous',
    'noise_floor_artifact',
    'insufficient_evidence',
]
"""#671 §3 — first-class decay behaviours; ambiguity and noise-floor
artifacts are states, not fitting errors."""

SingleSlopeAdequacyState = Literal[
    'single_slope_adequate',
    'multi_slope_supported',
    'multi_slope_suspected',
    'insufficient_range',
    'noise_limited',
    'indeterminate',
]
"""#671 §4 — the gate that decides whether a single-slope RT claim is
defensible for one region/position."""

MultiSlopeModelClass = Literal[
    'double_slope_model',
    'coupled_exponential_model',
    'non_exponential_decay_model',
]
"""#671 §6 — allowed multi-slope model classes; parameters are model-
internal, never mixed across classes."""

CoupledDecayQualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'single_slope_collapse_rejected',
    'insufficient_evidence',
    'not_evaluated',
]
"""The fail-closed per-scene qualification state."""

CoupledDecayFixtureId = Literal[
    'CPL10', 'CPL20', 'CPL30', 'CPL40', 'CPL50', 'CPL60', 'CPL70',
    'CPL80',
]
"""#671 fixtures: CPL10 same-material coupling control, CPL20
absorptive↔reverberant coupling, CPL30 door #573 state change, CPL40
curved decay measured, CPL50 single RT collapse attempt, CPL60
position-dependence surface, CPL70 low-frequency modal multi-slope,
CPL80 #570/#615 material intervention."""


# ----------------------------------------------------------------------
# Records

class CoupledDecayProfile(BaseModel):
    """The sealed coupled-scene identity (#671 §2).

    Declares which ``AcousticRegion`` members and which portals compose
    this coupled scene — including door/opening state — plus what the
    solver can represent. A solver that cannot exchange energy between
    regions cannot evidence a single-slope claim.
    """

    model_config = ConfigDict(frozen=True)

    region_refs: tuple[AuthorityRef, ...] = Field(min_length=1)
    portal_refs: tuple[AuthorityRef, ...] = ()
    """#1029 ``AcousticPortalCoupling`` refs including state (composes
    #573 door/opening)."""
    portal_state_declaration: str | None = None
    solver_ref: AuthorityRef | None = None
    solver_supports_energy_exchange: bool = False
    """Whether the pinned solver models inter-region energy exchange;
    ``False`` forbids a coupled-behaviour claim."""
    position_subset_refs: tuple[AuthorityRef, ...] = ()
    frequency_band_hz: tuple[float, float] | None = None
    material_profile_refs: tuple[AuthorityRef, ...] = ()
    """#570/#615 material evidence pins for the member regions."""

    @model_validator(mode='after')
    def _check(self) -> 'CoupledDecayProfile':
        for ref in self.region_refs:
            _require_ref_sha(ref, 'region_refs')
        for ref in self.portal_refs:
            _require_ref_sha(ref, 'portal_refs')
        _require_ref_sha(self.solver_ref, 'solver_ref')
        for ref in self.position_subset_refs:
            _require_ref_sha(ref, 'position_subset_refs')
        for ref in self.material_profile_refs:
            _require_ref_sha(ref, 'material_profile_refs')
        if len(self.region_refs) > 1 and not self.portal_refs:
            raise ValueError(
                'a multi-region profile must pin its portals (#671 §2)'
            )
        if self.frequency_band_hz is not None:
            low, high = self.frequency_band_hz
            _require_finite(low, 'frequency band low')
            _require_finite(high, 'frequency band high')
            if low >= high:
                raise ValueError('frequency_band_hz must be low < high')
        return self


class DecayComponent(BaseModel):
    """One early/late decay component inside a multi-slope fit (#671 §6)."""

    model_config = ConfigDict(frozen=True)

    role: Literal['early', 'late', 'intermediate']
    decay_rate_s: float = Field(gt=0.0)
    """Decay time (s) of this component — kept as time, not folded into
    a T30."""
    initial_level_db: float | None = None
    curvature_fraction: float | None = None
    """Declared share of the curve attributed to this component."""

    @model_validator(mode='after')
    def _check(self) -> 'DecayComponent':
        _require_finite(self.decay_rate_s, 'component decay_rate_s')
        for label in ('initial_level_db', 'curvature_fraction'):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, label)
        return self


class MultiSlopeDecayFit(BaseModel):
    """A declared multi-component decay fit (#671 §5/§6).

    Bound to the canonical raw decay artifact and its #676 processing
    refs — the fitted components are always traceable back to what was
    actually measured/modelled.
    """

    model_config = ConfigDict(frozen=True)

    fit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    raw_evidence_ref: AuthorityRef
    """Canonical decay evidence (RIR/EDC artifact) this fit consumes."""
    decay_processing_ref: AuthorityRef | None = None
    """#676 ``CadDecayFitRecord``/processing pin — noise floor and
    truncation decisions ride along."""
    model_class: MultiSlopeModelClass
    model_version: str = Field(min_length=1)
    components: tuple[DecayComponent, ...] = Field(min_length=2)
    position_ref: AuthorityRef | None = None
    """The position this fit is valid at — never generalized to the
    'room decay' (#671 §7)."""
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=COUPLED_DECAY_SCHEMA_VERSION, min_length=1
    )
    fit_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'raw_evidence_ref': self.raw_evidence_ref.model_dump(
                mode='json'
            ),
            'decay_processing_ref': (
                self.decay_processing_ref.model_dump(mode='json')
                if self.decay_processing_ref is not None
                else None
            ),
            'model_class': self.model_class,
            'model_version': self.model_version,
            'components': [
                c.model_dump(mode='json') for c in self.components
            ],
            'position_ref': (
                self.position_ref.model_dump(mode='json')
                if self.position_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'MultiSlopeDecayFit':
        _require_iso8601(self.declared_at_utc, 'fit declared_at_utc')
        _require_ref_sha(self.raw_evidence_ref, 'raw_evidence_ref')
        _require_ref_sha(
            self.decay_processing_ref, 'decay_processing_ref'
        )
        _require_ref_sha(self.position_ref, 'position_ref')
        roles = [c.role for c in self.components]
        if roles.count('early') > 1:
            raise ValueError(
                'a multi-slope fit keeps at most one early component'
            )
        expected = _hash(self.identity_payload())
        if self.fit_sha256 != expected:
            raise ValueError('multi-slope fit hash mismatch')
        if self.fit_id != _semantic_id('cplfit', expected):
            raise ValueError(
                'multi-slope fit id does not match its hash'
            )
        return self


def build_multi_slope_fit(
    *,
    document_id: str,
    raw_evidence_ref: AuthorityRef,
    model_class: MultiSlopeModelClass,
    model_version: str,
    components: Sequence[DecayComponent],
    decay_processing_ref: AuthorityRef | None = None,
    position_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> MultiSlopeDecayFit:
    """Seal one multi-slope decay fit."""
    return _seal(
        MultiSlopeDecayFit,
        {
            'document_id': document_id,
            'raw_evidence_ref': raw_evidence_ref.model_dump(mode='json'),
            'decay_processing_ref': (
                decay_processing_ref.model_dump(mode='json')
                if decay_processing_ref is not None
                else None
            ),
            'model_class': model_class,
            'model_version': model_version,
            'components': [
                c.model_dump(mode='json') for c in components
            ],
            'position_ref': (
                position_ref.model_dump(mode='json')
                if position_ref is not None
                else None
            ),
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'fit_id',
        'fit_sha256',
        'cplfit',
    )


def multi_slope_fit_binding(fit: MultiSlopeDecayFit) -> AuthorityRef:
    return AuthorityRef(
        kind='multi_slope_decay_fit',
        ref_id=fit.fit_id,
        ref_sha256=fit.fit_sha256,
    )


# ----------------------------------------------------------------------
# Single-slope adequacy gate

class SingleSlopeAdequacyAssessment(BaseModel):
    """Sealed per-region/position gate verdict (#671 §4)."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    raw_evidence_ref: AuthorityRef
    decay_processing_ref: AuthorityRef | None = None
    position_ref: AuthorityRef | None = None
    frequency_band_hz: tuple[float, float] | None = None
    adequacy_state: SingleSlopeAdequacyState
    curvature_evidence_declared: bool
    dynamic_range_db: float | None = None
    noise_floor_limited: bool
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=COUPLED_DECAY_EVALUATION_VERSION, min_length=1
    )
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'raw_evidence_ref': self.raw_evidence_ref.model_dump(
                mode='json'
            ),
            'decay_processing_ref': (
                self.decay_processing_ref.model_dump(mode='json')
                if self.decay_processing_ref is not None
                else None
            ),
            'position_ref': (
                self.position_ref.model_dump(mode='json')
                if self.position_ref is not None
                else None
            ),
            'frequency_band_hz': self.frequency_band_hz,
            'adequacy_state': self.adequacy_state,
            'curvature_evidence_declared': (
                self.curvature_evidence_declared
            ),
            'dynamic_range_db': self.dynamic_range_db,
            'noise_floor_limited': self.noise_floor_limited,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SingleSlopeAdequacyAssessment':
        _require_iso8601(
            self.evaluated_at_utc, 'assessment evaluated_at_utc'
        )
        _require_ref_sha(self.raw_evidence_ref, 'raw_evidence_ref')
        _require_ref_sha(
            self.decay_processing_ref, 'decay_processing_ref'
        )
        _require_ref_sha(self.position_ref, 'position_ref')
        if self.frequency_band_hz is not None:
            low, high = self.frequency_band_hz
            if low >= high:
                raise ValueError('frequency_band_hz must be low < high')
        if self.dynamic_range_db is not None:
            _require_finite(self.dynamic_range_db, 'dynamic_range_db')
        if (
            self.adequacy_state == 'single_slope_adequate'
            and self.curvature_evidence_declared
        ):
            raise ValueError(
                'declared curvature evidence forbids '
                'single_slope_adequate'
            )
        if (
            self.adequacy_state == 'single_slope_adequate'
            and self.noise_floor_limited
        ):
            raise ValueError(
                'a noise-floor-limited decay cannot reach '
                'single_slope_adequate'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('single-slope assessment hash mismatch')
        if self.assessment_id != _semantic_id('cplgate', expected):
            raise ValueError(
                'single-slope assessment id does not match its hash'
            )
        return self


def single_slope_adequacy_binding(
    assessment: SingleSlopeAdequacyAssessment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='single_slope_adequacy_assessment',
        ref_id=assessment.assessment_id,
        ref_sha256=assessment.assessment_sha256,
    )


def evaluate_single_slope_adequacy(
    document_id: str,
    *,
    raw_evidence_ref: AuthorityRef,
    decay_processing_ref: AuthorityRef | None = None,
    position_ref: AuthorityRef | None = None,
    frequency_band_hz: tuple[float, float] | None = None,
    curvature_evidence_declared: bool,
    dynamic_range_db: float | None,
    noise_floor_limited: bool,
    min_required_dynamic_range_db: float = 45.0,
    evaluated_at_utc: str | None = None,
) -> SingleSlopeAdequacyAssessment:
    """Fail-closed gate: may this decay be collapsed to one slope? (#671 §4)

    Rules:

    - Noise-floor limited (#676 evidence) → ``noise_limited``.
    - Missing/short dynamic range → ``insufficient_range`` — the tail
      that would reveal a second slope was never seen.
    - Declared curvature/multi-slope evidence →
      ``multi_slope_supported``; declared suspicion without a declared
      test → ``multi_slope_suspected``.
    - Only a decay with declared non-curved evidence and sufficient
      range reaches ``single_slope_adequate``.
    - Anything else → ``indeterminate``.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    _require_finite(
        min_required_dynamic_range_db, 'min_required_dynamic_range_db'
    )

    reasons: list[str] = []
    state: SingleSlopeAdequacyState

    if noise_floor_limited:
        state = 'noise_limited'
        reasons.append(
            'the usable decay range is noise-floor limited (#676 '
            'processing evidence) — a second slope could hide below '
            'the floor'
        )
    elif dynamic_range_db is None:
        state = 'insufficient_range'
        reasons.append(
            'no usable dynamic range was declared — the late tail was '
            'never observed'
        )
    elif dynamic_range_db < min_required_dynamic_range_db:
        state = 'insufficient_range'
        reasons.append(
            f'declared dynamic range {dynamic_range_db} dB is below the '
            f'{min_required_dynamic_range_db} dB needed to expose a '
            'second slope'
        )
    elif curvature_evidence_declared:
        state = 'multi_slope_supported'
        reasons.append(
            'curvature / multi-component evidence is declared for this '
            'decay'
        )
    else:
        state = 'single_slope_adequate'
        reasons.append(
            'declared evidence shows no curvature over sufficient '
            'range — single-slope collapse is defensible here only'
        )

    return _seal(
        SingleSlopeAdequacyAssessment,
        {
            'document_id': document_id,
            'raw_evidence_ref': raw_evidence_ref.model_dump(mode='json'),
            'decay_processing_ref': (
                decay_processing_ref.model_dump(mode='json')
                if decay_processing_ref is not None
                else None
            ),
            'position_ref': (
                position_ref.model_dump(mode='json')
                if position_ref is not None
                else None
            ),
            'frequency_band_hz': frequency_band_hz,
            'adequacy_state': state,
            'curvature_evidence_declared': curvature_evidence_declared,
            'dynamic_range_db': dynamic_range_db,
            'noise_floor_limited': noise_floor_limited,
            'reasons': tuple(reasons),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'assessment_id',
        'assessment_sha256',
        'cplgate',
    )


# ----------------------------------------------------------------------
# Inter-region qualification

class InterRegionEnergyDecayQualification(BaseModel):
    """The sealed coupled-decay qualification verdict (#671 §8)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile: CoupledDecayProfile
    behavior_state: DecayBehaviorState
    adequacy_refs: tuple[AuthorityRef, ...] = ()
    multi_slope_fit_refs: tuple[AuthorityRef, ...] = ()
    state: CoupledDecayQualificationState
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=COUPLED_DECAY_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile': self.profile.model_dump(mode='json'),
            'behavior_state': self.behavior_state,
            'adequacy_refs': [
                r.model_dump(mode='json') for r in self.adequacy_refs
            ],
            'multi_slope_fit_refs': [
                r.model_dump(mode='json')
                for r in self.multi_slope_fit_refs
            ],
            'state': self.state,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'InterRegionEnergyDecayQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        for ref in self.adequacy_refs:
            _require_ref_sha(ref, 'adequacy_refs')
        for ref in self.multi_slope_fit_refs:
            _require_ref_sha(ref, 'multi_slope_fit_refs')
        if (
            self.state == 'qualified'
            and self.behavior_state
            in ('mixed_ambiguous', 'noise_floor_artifact',
                'insufficient_evidence')
        ):
            raise ValueError(
                f'behavior {self.behavior_state} cannot reach qualified'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('coupled-decay qualification hash mismatch')
        if self.qualification_id != _semantic_id('cplqual', expected):
            raise ValueError(
                'coupled-decay qualification id does not match its '
                'hash'
            )
        return self


def coupled_decay_binding(
    qualification: InterRegionEnergyDecayQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='inter_region_energy_decay_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


def evaluate_coupled_decay(
    document_id: str,
    profile: CoupledDecayProfile,
    adequacy_assessments: Sequence[SingleSlopeAdequacyAssessment] = (),
    multi_slope_fits: Sequence[MultiSlopeDecayFit] = (),
    *,
    evaluated_at_utc: str | None = None,
) -> InterRegionEnergyDecayQualification:
    """Fail-closed coupled-scene decay qualification (#671).

    Rules:

    - Multiple regions but the solver cannot exchange energy →
      ``single_slope_collapse_rejected`` for coupled claims; the
      scene's coupling is un-modelled.
    - No adequacy evidence at all → ``insufficient_evidence``.
    - Any ``noise_limited``/``insufficient_range`` gate →
      ``qualified_with_limitations`` naming the weak positions; the
      other positions' results stand on their own.
    - ``multi_slope_supported``/``multi_slope_suspected`` requires a
      bound multi-slope fit for the scene to be ``qualified``;
      without one the behavior is ``multi_slope_suspected`` and state
      ``qualified_with_limitations``.
    - All-adequate single slopes → ``qualified`` with
      ``single_exponential_decay`` behavior *for the declared
      positions only* — never "the room".
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []
    coupled = len(profile.region_refs) > 1

    if not adequacy_assessments:
        state: CoupledDecayQualificationState = 'insufficient_evidence'
        behavior: DecayBehaviorState = 'insufficient_evidence'
        reasons.append(
            'no single-slope adequacy evidence was declared for any '
            'region/position'
        )
        adequacy_refs: tuple[AuthorityRef, ...] = ()
        fit_refs: tuple[AuthorityRef, ...] = ()
    elif coupled and not profile.solver_supports_energy_exchange:
        state = 'single_slope_collapse_rejected'
        behavior = 'mixed_ambiguous'
        reasons.append(
            'the declared solver cannot exchange energy between the '
            'pinned regions — the coupled scene cannot evidence a '
            'single-slope claim'
        )
        adequacy_refs = tuple(
            single_slope_adequacy_binding(a)
            for a in adequacy_assessments
        )
        fit_refs = tuple(
            multi_slope_fit_binding(f) for f in multi_slope_fits
        )
    else:
        adequacy_states = {
            a.adequacy_state for a in adequacy_assessments
        }
        adequacy_refs = tuple(
            single_slope_adequacy_binding(a)
            for a in adequacy_assessments
        )
        fit_refs = tuple(
            multi_slope_fit_binding(f) for f in multi_slope_fits
        )

        weak = adequacy_states & {'noise_limited', 'insufficient_range'}

        if 'multi_slope_supported' in adequacy_states:
            if multi_slope_fits:
                state = 'qualified'
                classes = {f.model_class for f in multi_slope_fits}
                behavior = (
                    'coupled_volume_double_slope'
                    if classes == {'double_slope_model'}
                    else 'coupled_volume_multi_slope'
                )
            else:
                state = 'qualified_with_limitations'
                behavior = 'mixed_ambiguous'
                reasons.append(
                    'multi-slope evidence is declared but no bound '
                    'multi-slope fit was provided'
                )
        elif 'multi_slope_suspected' in adequacy_states:
            state = 'qualified_with_limitations'
            behavior = 'mixed_ambiguous'
            limitations.append(
                'multi-slope decay suspected — retained as suspected, '
                'not collapsed'
            )
        elif adequacy_states == {'single_slope_adequate'}:
            state = 'qualified'
            behavior = 'single_exponential_decay'
            limitations.append(
                'single-slope qualification applies only to the '
                'declared positions/bands — it is not a room-level RT '
                'claim'
            )
        elif weak and adequacy_states <= (
            {'single_slope_adequate'} | weak
        ):
            state = 'qualified_with_limitations'
            behavior = 'single_exponential_decay'
            limitations.append(
                'some positions are range/noise limited — single-slope '
                'is only defensible at the adequate positions'
            )
        else:
            state = 'qualified_with_limitations'
            behavior = 'mixed_ambiguous'
            limitations.append(
                'mixed adequacy evidence — see per-position gates'
            )

        if weak:
            limitations.append(
                'noise-limited / insufficient-range positions retained '
                'as weak evidence'
            )

    return _seal(
        InterRegionEnergyDecayQualification,
        {
            'document_id': document_id,
            'profile': profile.model_dump(mode='json'),
            'behavior_state': behavior,
            'adequacy_refs': [
                r.model_dump(mode='json') for r in adequacy_refs
            ],
            'multi_slope_fit_refs': [
                r.model_dump(mode='json') for r in fit_refs
            ],
            'state': state,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'cplqual',
    )
