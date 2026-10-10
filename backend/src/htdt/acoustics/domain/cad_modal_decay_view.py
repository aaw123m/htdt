"""Time-frequency modal-decay authority (#706, REV58-VALIDMETH).

Waterfall/spectrogram displays are presentation. When decay evidence is
produced through a time-frequency transform, the **transform is
authority**: method, window, hop, frequency grid, wavelet parameters,
smoothing, analysis range and normalization are pinned, and the
time↔frequency resolution is declared with its trustworthy range. A
decay value read off an undocumented waterfall is not evidence.

Scope discipline (#706):

- Transform identity is exact: STFT / CSD moving-window IR /
  continuous wavelet / Morlet CWT / Stockwell / optimized Stockwell /
  burst-decay wavelet envelope / filtered modal decay / custom — each
  with its full parameter set; a parameter hash pins the exact
  configuration (#706 §2/§3).
- Time and frequency resolution are declared together — a finer
  frequency grid is never read as better time resolution; the
  trustworthy range excludes edge-effect zones (#706 §4).
- Stages are separate records: transformed field → mode candidate →
  ridge → envelope → fit → result. A waterfall image is downstream
  presentation, never the canonical artifact; REW-style screenshots
  are never canonical evidence (#706 §5/§10).
- Mode overlap is honest: isolated / partially overlapped /
  unresolved multiple modes / ridge-crossing ambiguous / insufficient
  frequency resolution are first-class states (#706 §6).
- Envelope extraction and fit semantics are declared (energy vs
  magnitude, smoothing), and noise-floor / truncation uncertainty
  from #676/#572 stays attached to the fitted decay (#706 §7/§8).
- A single-exponential modal fit is valid only where mode isolation is
  supported; beating/interference → multi-component semantics compose
  #671 (#706 §9).
- Compare against #674/#566 only as separate observables — frequency,
  damping and shape are never fused into one score (#706 §11).

Records:

- :class:`TimeFrequencyDecayTransform` — the sealed transform identity
  and resolution declaration.
- :class:`ModalDecayObservation` — one sealed mode-candidate decay
  observation bound to its transform.
- :class:`ModalDecayQualification` — the fail-closed verdict from
  :func:`evaluate_modal_decay_qualification`.

Literature basis
----------------
- Stockwell-transform modal decay estimation, Measurement 203 (2022)
  111941. Optimized S-transform for room modal decay — transform
  parameters change the measured decay.
- Lee (2002), J. Sound Vib. — DOI 10.1006/jsvi.2001.4035. Wavelet /
  time-frequency damping estimation; resolution trade-offs.
- Prato et al. (2016), Appl. Acoust. — DOI 10.1016/j.apacoust.2016.03.041.
  Room modal decay under strong mode overlap — single-exponential
  assumption fails in overlapped bands.
- Welti (2015), JASA — DOI 10.1121/1.4908217. Time-frequency limits on
  low-frequency decay estimation.
"""

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_registry import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload
from ...clock import utc_now_iso as _utc_now

MODAL_DECAY_SCHEMA_VERSION = 'modal-decay-view-1'
MODAL_DECAY_EVALUATION_VERSION = 'modal-decay-view-eval-1'

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

DecayTransformKind = Literal[
    'stft_fixed_window',
    'csd_moving_ir_window',
    'continuous_wavelet',
    'morlet_cwt',
    'stockwell_s_transform',
    'optimized_stockwell',
    'burst_decay_wavelet_envelope',
    'filtered_modal_decay',
    'custom_validated',
]
"""#706 §2 — transform taxonomy; each has different
time↔frequency resolution semantics."""

ModeOverlapState = Literal[
    'isolated_mode',
    'partially_overlapped',
    'unresolved_multiple_modes',
    'ridge_crossing_ambiguous',
    'insufficient_frequency_resolution',
]
"""#706 §6 — overlap states are honest, not averaged."""

EnvelopeSemantic = Literal[
    'energy_envelope',
    'magnitude_envelope',
    'log_energy_envelope',
]
"""What the extracted envelope actually is — energy, magnitude or
log-energy are not interchangeable decay curves."""

ModalDecayFitModel = Literal[
    'single_exponential',
    'multi_exponential_declared',
]
"""#706 §9 — a single-exponential fit is valid only where isolation is
supported; beating/interference needs declared multi-component
semantics (compose #671)."""

ModalDecayQualificationState = Literal[
    'qualified',
    'qualified_with_limitations',
    'insufficient_evidence',
    'transform_incompatible',
    'overlap_unresolved',
    'noise_or_truncation_limited',
]
"""The fail-closed verdict states."""

ModalDecayFixtureId = Literal[
    'MDT10', 'MDT20', 'MDT30', 'MDT40', 'MDT50', 'MDT60', 'MDT70',
    'MDT80',
]
"""#706 fixtures: MDT10 isolated single-mode decay, MDT20 two close
modes (overlap), MDT30 ridge crossing, MDT40 noise-floor limited
(#676), MDT50 transform-parameter sensitivity, MDT60 multi-position
roles, MDT70 window-resolution misread, MDT80 beating / multi-
component (compose #671)."""

# ----------------------------------------------------------------------
# Transform identity

class TimeFrequencyDecayTransform(BaseModel):
    """The sealed time-frequency transform identity (#706 §2/§3/§4).

    Every parameter that changes the extracted decay is pinned here; a
    waterfall rendered from a different configuration is a different
    transform.
    """

    model_config = ConfigDict(frozen=True)

    transform_kind: DecayTransformKind
    window: str | None = None
    """Window family + length in canonical text, e.g.
    ``hann:256``."""
    hop: str | None = None
    frequency_grid: str | None = None
    """Frequency grid declaration (start/stop/step or vector ref)."""
    wavelet_parameters: str | None = None
    """Wavelet/mother + scale parameters in canonical text."""
    smoothing: str | None = None
    analysis_range_s: tuple[float, float] | None = None
    normalization: str = Field(min_length=1)
    parameter_hash: str = Field(pattern=_SHA256_PATTERN)
    """Content hash of the exact transform parameter artifact."""
    time_resolution_s: float = Field(gt=0.0)
    frequency_resolution_hz: float = Field(gt=0.0)
    trustworthy_time_range_s: tuple[float, float] | None = None
    trustworthy_frequency_range_hz: tuple[float, float] | None = None
    edge_effects_declared: str | None = None
    """Description of the declared edge-effect / cone-of-influence
    zone excluded from the trustworthy range."""

    @model_validator(mode='after')
    def _check(self) -> 'TimeFrequencyDecayTransform':
        _require_finite(
            self.time_resolution_s, 'time_resolution_s'
        )
        _require_finite(
            self.frequency_resolution_hz, 'frequency_resolution_hz'
        )
        for label, pair in (
            ('analysis_range_s', self.analysis_range_s),
            ('trustworthy_time_range_s', self.trustworthy_time_range_s),
            (
                'trustworthy_frequency_range_hz',
                self.trustworthy_frequency_range_hz,
            ),
        ):
            if pair is not None:
                low, high = pair
                for bound in pair:
                    _require_finite(bound, label)
                if low >= high:
                    raise ValueError(f'{label} must be low < high')
        for label, value in (
            ('window', self.window),
            ('hop', self.hop),
            ('frequency_grid', self.frequency_grid),
            ('wavelet_parameters', self.wavelet_parameters),
        ):
            if self.transform_kind in (
                'stft_fixed_window', 'csd_moving_ir_window'
            ) and label in ('window', 'hop', 'frequency_grid') and (
                value is None
            ):
                raise ValueError(
                    f'{self.transform_kind} requires a declared '
                    f'{label}'
                )
        if self.transform_kind in (
            'continuous_wavelet', 'morlet_cwt', 'stockwell_s_transform',
            'optimized_stockwell', 'burst_decay_wavelet_envelope',
        ) and self.wavelet_parameters is None:
            raise ValueError(
                f'{self.transform_kind} requires declared wavelet '
                'parameters'
            )
        return self

def transform_binding(
    transform: TimeFrequencyDecayTransform,
) -> AuthorityRef:
    """Reference a transform by content — the transform record itself is
    its own identity."""
    payload = transform.model_dump(mode='json')
    digest = _hash(payload)
    return AuthorityRef(
        kind='time_frequency_decay_transform',
        ref_id=_semantic_id('tfx', digest),
        ref_sha256=digest,
    )

# ----------------------------------------------------------------------
# Observation

class ModalDecayObservation(BaseModel):
    """One sealed mode-candidate decay observation (#706 §5/§6/§7).

    Bound to its transform; stages (field→candidate→ridge→envelope→fit)
    are recorded inside the observation so nothing is silently promoted
    from a picture to a number.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    transform: TimeFrequencyDecayTransform
    raw_evidence_ref: AuthorityRef
    """Canonical measured/modelled field artifact (RIR etc.)."""
    processing_refs: tuple[AuthorityRef, ...] = ()
    """#676/#572 noise-floor / truncation decisions that constrain the
    decay range."""
    mode_candidate_center_hz: float = Field(gt=0.0)
    overlap_state: ModeOverlapState
    envelope_semantic: EnvelopeSemantic
    ridge_range_hz: tuple[float, float] | None = None
    """Ridge extent inside the transform."""
    fit_model: ModalDecayFitModel
    fitted_decay_s: float | None = Field(default=None, gt=0.0)
    fit_uncertainty_s: float | None = Field(default=None, ge=0.0)
    position_ref: AuthorityRef | None = None
    """The receiver position this observation belongs to."""
    smoothing_declared: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=MODAL_DECAY_SCHEMA_VERSION, min_length=1
    )
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'transform': self.transform.model_dump(mode='json'),
            'raw_evidence_ref': self.raw_evidence_ref.model_dump(
                mode='json'
            ),
            'processing_refs': [
                r.model_dump(mode='json') for r in self.processing_refs
            ],
            'mode_candidate_center_hz': self.mode_candidate_center_hz,
            'overlap_state': self.overlap_state,
            'envelope_semantic': self.envelope_semantic,
            'ridge_range_hz': self.ridge_range_hz,
            'fit_model': self.fit_model,
            'fitted_decay_s': self.fitted_decay_s,
            'fit_uncertainty_s': self.fit_uncertainty_s,
            'position_ref': (
                self.position_ref.model_dump(mode='json')
                if self.position_ref is not None
                else None
            ),
            'smoothing_declared': self.smoothing_declared,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ModalDecayObservation':
        _require_iso8601(
            self.declared_at_utc, 'observation declared_at_utc'
        )
        _require_ref_sha(self.raw_evidence_ref, 'raw_evidence_ref')
        for ref in self.processing_refs:
            _require_ref_sha(ref, 'processing_refs')
        _require_ref_sha(self.position_ref, 'position_ref')
        _require_finite(
            self.mode_candidate_center_hz, 'mode_candidate_center_hz'
        )
        if self.fitted_decay_s is not None:
            _require_finite(self.fitted_decay_s, 'fitted_decay_s')
        if self.fit_uncertainty_s is not None:
            _require_finite(
                self.fit_uncertainty_s, 'fit_uncertainty_s'
            )
        if self.ridge_range_hz is not None:
            low, high = self.ridge_range_hz
            for bound in (low, high):
                _require_finite(bound, 'ridge_range_hz')
            if low >= high:
                raise ValueError('ridge_range_hz must be low < high')
            width = high - low
            if width < self.transform.frequency_resolution_hz:
                raise ValueError(
                    'ridge width cannot be narrower than the declared '
                    'frequency resolution'
                )
        if (
            self.fit_model == 'single_exponential'
            and self.overlap_state
            in ('unresolved_multiple_modes', 'ridge_crossing_ambiguous')
        ):
            raise ValueError(
                'a single-exponential fit is invalid under unresolved '
                'overlap — declare a multi-component model (compose '
                '#671)'
            )
        return self

def build_modal_decay_observation(
    *,
    document_id: str,
    transform: TimeFrequencyDecayTransform,
    raw_evidence_ref: AuthorityRef,
    mode_candidate_center_hz: float,
    overlap_state: ModeOverlapState,
    envelope_semantic: EnvelopeSemantic,
    fit_model: ModalDecayFitModel,
    processing_refs: Sequence[AuthorityRef] = (),
    ridge_range_hz: tuple[float, float] | None = None,
    fitted_decay_s: float | None = None,
    fit_uncertainty_s: float | None = None,
    position_ref: AuthorityRef | None = None,
    smoothing_declared: str | None = None,
    declared_at_utc: str | None = None,
) -> ModalDecayObservation:
    """Seal one modal-decay observation."""
    return _seal(
        ModalDecayObservation,
        {
            'document_id': document_id,
            'transform': transform.model_dump(mode='json'),
            'raw_evidence_ref': raw_evidence_ref.model_dump(mode='json'),
            'processing_refs': [
                r.model_dump(mode='json') for r in processing_refs
            ],
            'mode_candidate_center_hz': mode_candidate_center_hz,
            'overlap_state': overlap_state,
            'envelope_semantic': envelope_semantic,
            'ridge_range_hz': ridge_range_hz,
            'fit_model': fit_model,
            'fitted_decay_s': fitted_decay_s,
            'fit_uncertainty_s': fit_uncertainty_s,
            'position_ref': (
                position_ref.model_dump(mode='json')
                if position_ref is not None
                else None
            ),
            'smoothing_declared': smoothing_declared,
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'observation_id',
        'observation_sha256',
        'mdtobs',
    )

def modal_decay_observation_binding(
    observation: ModalDecayObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='modal_decay_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )

# ----------------------------------------------------------------------
# Qualification

class ModalDecayQualification(BaseModel):
    """The sealed fail-closed modal-decay verdict (#706 §12)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_ref: AuthorityRef
    state: ModalDecayQualificationState
    decay_trustworthy: bool
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=MODAL_DECAY_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'observation_ref': self.observation_ref.model_dump(
                mode='json'
            ),
            'state': self.state,
            'decay_trustworthy': self.decay_trustworthy,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ModalDecayQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.observation_ref.kind != 'modal_decay_observation':
            raise ValueError(
                "observation_ref must pin a 'modal_decay_observation'"
            )
        _require_ref_sha(self.observation_ref, 'observation_ref')
        if self.state == 'qualified' and not self.decay_trustworthy:
            raise ValueError(
                'qualified requires decay_trustworthy'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('modal-decay qualification hash mismatch')
        if self.qualification_id != _semantic_id('mdtqual', expected):
            raise ValueError(
                'modal-decay qualification id does not match its hash'
            )
        return self

def modal_decay_qualification_binding(
    qualification: ModalDecayQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='modal_decay_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )

def evaluate_modal_decay_qualification(
    document_id: str,
    observation: ModalDecayObservation,
    *,
    noise_floor_limited: bool = False,
    truncation_limited: bool = False,
    evaluated_at_utc: str | None = None,
) -> ModalDecayQualification:
    """Fail-closed modal-decay qualification (#706).

    Rules:

    - No fitted decay → ``insufficient_evidence`` — a picture is not a
      number.
    - ``unresolved_multiple_modes`` / ``ridge_crossing_ambiguous`` /
      ``insufficient_frequency_resolution`` → ``overlap_unresolved``;
      the observed decay is a composite, not a mode decay.
    - Noise-floor or truncation-limited processing evidence →
      ``noise_or_truncation_limited``; the fit range never reached the
      true tail.
    - ``partially_overlapped`` or missing uncertainty →
      ``qualified_with_limitations``.
    - Only an isolated mode with a fitted decay, declared uncertainty
      and clean processing reaches ``qualified``.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []

    if observation.fitted_decay_s is None:
        state: ModalDecayQualificationState = 'insufficient_evidence'
        reasons.append(
            'no fitted decay was produced — the transform display '
            'itself is presentation, not evidence'
        )
        trustworthy = False
    elif observation.overlap_state in (
        'unresolved_multiple_modes',
        'ridge_crossing_ambiguous',
        'insufficient_frequency_resolution',
    ):
        state = 'overlap_unresolved'
        trustworthy = False
        reasons.append(
            f'overlap state {observation.overlap_state} — the fitted '
            'decay is a composite or ambiguous ridge, not a single '
            'mode decay'
        )
    elif noise_floor_limited or truncation_limited:
        state = 'noise_or_truncation_limited'
        trustworthy = False
        reasons.append(
            'processing evidence limits the usable decay range (#676/'
            '#572) — the fitted slope may never have reached the true '
            'tail'
        )
    else:
        trustworthy = True
        if observation.overlap_state == 'partially_overlapped':
            state = 'qualified_with_limitations'
            limitations.append(
                'neighbouring energy partially overlaps the candidate '
                'ridge — decay retains a bias bound'
            )
        elif observation.fit_uncertainty_s is None:
            state = 'qualified_with_limitations'
            limitations.append(
                'no fit uncertainty was declared — precision is '
                'unknown'
            )
        else:
            state = 'qualified'

    if observation.fit_model == 'multi_exponential_declared':
        limitations.append(
            'multi-component fit — component semantics compose #671 '
            'coupled-decay authority'
        )

    return _seal(
        ModalDecayQualification,
        {
            'document_id': document_id,
            'observation_ref': modal_decay_observation_binding(
                observation
            ).model_dump(mode='json'),
            'state': state,
            'decay_trustworthy': trustworthy,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'mdtqual',
    )
