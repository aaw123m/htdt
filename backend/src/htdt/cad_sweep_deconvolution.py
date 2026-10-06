"""Swept-sine deconvolution / harmonic-separation authority (#697,
REV58-MEASCHAIN).

An exponential/log swept-sine recording does not become a trustworthy
*linear* impulse response merely by applying an inverse filter. The exact
sweep/deconvolution method, clock/time alignment, harmonic separation,
extraction windows and residual nonlinear artifacts decide which parts of
the recovered IR are physically eligible.

This module owns the typed recovery path — never one opaque
``impulse_response.wav``:

- :class:`CadSweepDeconvolutionSpec` — the sealed deconvolution
  specification: the exact #608 sweep pin, sweep law/span/duration/rate,
  algorithm family and version, inverse-filter construction, normalization,
  window/zero-padding/regularization, resampling declaration, time-origin
  convention and output scale. Two log sweeps with different duration or
  span are different measurement methods even when the UI label matches.
- :class:`CadHarmonicImpulseComponent` — one declared/recovered nonlinear
  order: its expected time offset derived from the exact sweep law, the
  extraction window, the fundamental band over which the order is valid,
  and its overlap state relative to the causal linear region.
- :class:`CadRecoveredImpulseResponse` — the sealed recovered-IR record
  binding stimulus → raw capture → spec → full response → linear/harmonic
  extracts. Raw capture and the full deconvolution result stay immutable;
  extracting the linear IR never overwrites the full response.
- :class:`CadLinearIRCapability` — the fail-closed verdict: contamination
  state and per-metric eligibility (FR / direct arrival / early
  reflections / decay / clarity / absolute phase).

Composition:

- #608 supplies exact stimulus identity; #609 supplies the clock /
  synchronization prerequisite — exact higher-harmonic analysis under
  asynchronous clocks is a LIMITED/INELIGIBLE result, never silently
  produced.
- #575 transformations carry windowing/gating as first-class derived
  steps; changing the extraction window creates a different derived
  result.
- #695 chain linearity gates distortion attribution: harmonic impulse
  components from an overloaded acquisition chain may be measurement-chain
  products — successful harmonic separation never proves the chain was
  linear.
- Imported external IRs (REW & friends) keep their unknown deconvolution
  provenance: ``imported_final_only`` records source tool metadata and
  never fabricates a sweep/inverse/window history.

Honesty rules baked into the models and the evaluator:

- Nonlinear components are not assumed to sit universally before t=0 —
  causal contamination is evaluated, never assumed away.
- Harmonic order N carries a fundamental validity band: energy at
  N·f must lie inside the excited/captured bandwidth — HDn beyond that
  band fails closed.
- Harmonic order offsets are derived from the exact sweep law, never
  inferred from hand-drawn windows.
- ``imported_final_only`` cannot claim derived provenance fields, and a
  derived record cannot drop its spec/capture pins.

Literature basis
----------------
- Farina, A., *Simultaneous Measurement of Impulse Response and
  Distortion with a Swept-Sine Technique*, AES 108th Convention, Paper
  5093 (2000): exponential-sweep deconvolution separates the linear IR
  and harmonic-distortion impulse responses in time; harmonic order n
  lands at Δt_n = −T·ln(n)/ln(f₂/f₁) relative to the linear peak.
- Novak, Lotton & Simon, *Synchronized Swept-Sine: Theory, Application,
  and Implementation*, JAES 63(10) (2015): playback/capture
  synchronization is required for proper higher-harmonic analysis.
- Ćirić et al., *On the effects of nonlinearities in room impulse
  response measurements with exponential sweeps*, Applied Acoustics
  74(3) (2013): some nonlinear artifacts intrude into the *causal* part
  of the recovered IR and bias room-acoustic parameters — "harmonics are
  always safely before t=0" is not a defensible assumption.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite, log
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SWEEP_DECONV_SCHEMA_VERSION = 'measchain-ess-1'
SWEEP_DECONV_EVALUATION_VERSION = 'measchain-ess-eval-1'

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


def _require_sha(value: str | None, label: str) -> None:
    if value is None:
        return
    if len(value) != 64:
        raise ValueError(f'{label} must be 64 hex characters')
    int(value, 16)


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#697)
# ---------------------------------------------------------------------------

SweepLaw = Literal['exponential', 'linear', 'other', 'unknown']

DeconvolutionAlgorithm = Literal[
    'farina_inverse_filter',
    'regularized_division',
    'time_domain_least_squares',
    'synchronized_swept_sine',
    'imported_unknown',
    'other',
    'unknown',
]

InverseConstruction = Literal[
    'time_reversal_with_amplitude_taper',
    'frequency_domain_inverse',
    'regularized_frequency_domain_inverse',
    'imported_unknown',
    'other',
    'unknown',
]

TimeOriginConvention = Literal[
    'linear_peak_at_t0',
    'capture_start',
    'reference_signal_aligned',
    'unknown',
]

IRProvenanceClass = Literal[
    'derived_full_provenance',
    'imported_with_metadata',
    'imported_final_only',
]

HarmonicOverlapState = Literal[
    'separated',
    'partial_overlap',
    'contaminates_causal',
    'inseparable',
    'unknown',
]

IRContaminationState = Literal[
    'linear_ir_clean_within_declared_window',
    'nonlinear_components_separated',
    'partial_overlap',
    'causal_nonlinear_contamination_risk',
    'inseparable',
    'insufficient_evidence',
]

IRMetricCapability = Literal[
    'fr_valid',
    'direct_arrival_valid',
    'early_reflection_valid',
    'decay_metric_valid',
    'clarity_valid',
    'absolute_phase_valid',
]

ALL_IR_METRIC_CAPABILITIES: tuple[IRMetricCapability, ...] = (
    'fr_valid',
    'direct_arrival_valid',
    'early_reflection_valid',
    'decay_metric_valid',
    'clarity_valid',
    'absolute_phase_valid',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']

ClockGateState = Literal[
    'synchronized',
    'unsynchronized_declared',
    'unassessed',
    'unknown',
]

ChainGateState = Literal[
    'qualified',
    'overload_suspected',
    'overload_observed',
    'unassessed',
    'unknown',
]


# ---------------------------------------------------------------------------
# Embedded blocks
# ---------------------------------------------------------------------------


class CadExtractionWindow(BaseModel):
    """A declared extraction window over the recovered response.

    Windows are first-class transforms: boundaries, taper and the
    alignment reference are all part of the derived result's identity.
    """

    model_config = ConfigDict(frozen=True)

    window_start_s: float
    window_end_s: float
    taper: str | None = None
    alignment_reference: str | None = None

    @model_validator(mode='after')
    def valid_window(self) -> 'CadExtractionWindow':
        _require_finite(self.window_start_s, 'window start')
        _require_finite(self.window_end_s, 'window end')
        if self.window_end_s <= self.window_start_s:
            raise ValueError('extraction window must be ascending')
        return self

    def contains_s(self, t: float) -> bool:
        return self.window_start_s <= t <= self.window_end_s

    def overlaps(self, other: 'CadExtractionWindow') -> bool:
        return (
            self.window_start_s < other.window_end_s
            and other.window_start_s < self.window_end_s
        )


# ---------------------------------------------------------------------------
# Deconvolution spec
# ---------------------------------------------------------------------------


class CadSweepDeconvolutionSpec(BaseModel):
    """Sealed swept-sine deconvolution specification.

    ``sweep_law``/``sweep_f_start_hz``/``sweep_f_end_hz``/
    ``sweep_duration_s``/``sweep_sample_rate_hz`` restate the exact sweep
    parameters the harmonic-offset derivation runs on; ``stimulus_ref``
    pins the #608 stimulus identity — the two must describe the same
    sweep. All algorithmic choices (inverse construction, normalization,
    padding, regularization, resampling, time origin, output scale) are
    reproducible inputs, not implied defaults.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stimulus_ref: AuthorityRef
    sweep_law: SweepLaw
    sweep_f_start_hz: float | None = None
    sweep_f_end_hz: float | None = None
    sweep_duration_s: float | None = None
    sweep_sample_rate_hz: float | None = None
    algorithm: DeconvolutionAlgorithm
    implementation_version: str | None = None
    inverse_construction: InverseConstruction = 'unknown'
    normalization: str | None = None
    fft_block_size: int | None = None
    zero_padding: str | None = None
    regularization: str | None = None
    resampling_declaration: str | None = None
    time_origin_convention: TimeOriginConvention = 'unknown'
    output_scale: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'CadSweepDeconvolutionSpec':
        _require_iso8601(self.declared_at_utc, 'spec declared_at_utc')
        if self.stimulus_ref.ref_sha256 is None:
            raise ValueError(
                'the #608 stimulus pin must carry its sha256 — a sweep '
                'label is never an identity'
            )
        for label, value in (
            ('sweep_f_start_hz', self.sweep_f_start_hz),
            ('sweep_f_end_hz', self.sweep_f_end_hz),
            ('sweep_duration_s', self.sweep_duration_s),
            ('sweep_sample_rate_hz', self.sweep_sample_rate_hz),
        ):
            if value is not None:
                _require_finite(value, f'spec {label}')
                if value <= 0:
                    raise ValueError(f'spec {label} must be positive')
        if (
            self.sweep_f_start_hz is not None
            and self.sweep_f_end_hz is not None
            and self.sweep_f_end_hz <= self.sweep_f_start_hz
        ):
            raise ValueError('sweep band must be ascending')
        if self.fft_block_size is not None and self.fft_block_size <= 0:
            raise ValueError('fft_block_size must be positive')
        # Harmonic-offset derivation needs the full law: law + band +
        # duration. Partial law is allowed but never silently "derived".
        law_complete = (
            self.sweep_law == 'exponential'
            and self.sweep_f_start_hz is not None
            and self.sweep_f_end_hz is not None
            and self.sweep_duration_s is not None
        )
        if self.algorithm == 'farina_inverse_filter' and (
            self.sweep_law != 'exponential' and self.sweep_law != 'unknown'
        ):
            raise ValueError(
                'farina_inverse_filter pairs with an exponential sweep — '
                'a different law under that label is a different method'
            )
        if self.sweep_law == 'exponential' and not law_complete:
            # Allowed to persist, but honest: the missing law numbers are
            # exactly what blocks harmonic derivation at evaluation.
            pass
        expected = _hash(self.identity_payload())
        if self.spec_sha256 != expected:
            raise ValueError('deconvolution spec hash mismatch')
        if self.spec_id != _semantic_id('swspec', expected):
            raise ValueError('spec id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stimulus_ref': self.stimulus_ref.model_dump(mode='json'),
            'sweep_law': self.sweep_law,
            'sweep_f_start_hz': self.sweep_f_start_hz,
            'sweep_f_end_hz': self.sweep_f_end_hz,
            'sweep_duration_s': self.sweep_duration_s,
            'sweep_sample_rate_hz': self.sweep_sample_rate_hz,
            'algorithm': self.algorithm,
            'implementation_version': self.implementation_version,
            'inverse_construction': self.inverse_construction,
            'normalization': self.normalization,
            'fft_block_size': self.fft_block_size,
            'zero_padding': self.zero_padding,
            'regularization': self.regularization,
            'resampling_declaration': self.resampling_declaration,
            'time_origin_convention': self.time_origin_convention,
            'output_scale': self.output_scale,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def harmonic_offset_s(self, order: int) -> float | None:
        """Expected time offset of harmonic order ``order`` relative to the
        linear response, for an exponential sweep.

        Farina/Novak: an exponential sweep from f₁→f₂ over duration T
        places the n-th harmonic impulse at Δt_n = −T·ln(n)/ln(f₂/f₁).
        ``None`` when the sweep law is not fully declared — offsets are
        derived from the exact law, never from hand-drawn windows.
        """
        if order < 2:
            raise ValueError('harmonic order must be >= 2')
        if self.sweep_law != 'exponential':
            return None
        if (
            self.sweep_f_start_hz is None
            or self.sweep_f_end_hz is None
            or self.sweep_duration_s is None
        ):
            return None
        return (
            -self.sweep_duration_s
            * log(order)
            / log(self.sweep_f_end_hz / self.sweep_f_start_hz)
        )

    def harmonic_valid_band(self, order: int) -> tuple[float, float] | None:
        """Fundamental band over which harmonic order ``order`` stays
        inside the excited bandwidth: f ∈ [f₁, f₂/N]."""
        if order < 2:
            raise ValueError('harmonic order must be >= 2')
        if self.sweep_f_start_hz is None or self.sweep_f_end_hz is None:
            return None
        high = self.sweep_f_end_hz / order
        if high <= self.sweep_f_start_hz:
            return None
        return (self.sweep_f_start_hz, high)


def deconv_spec_binding(spec: CadSweepDeconvolutionSpec) -> AuthorityRef:
    return AuthorityRef(
        kind='sweep_deconvolution_spec',
        ref_id=spec.spec_id,
        ref_sha256=spec.spec_sha256,
    )


# ---------------------------------------------------------------------------
# Harmonic impulse components
# ---------------------------------------------------------------------------


class CadHarmonicImpulseComponent(BaseModel):
    """One declared/recovered nonlinear-order component of a deconvolution.

    ``expected_offset_s`` must equal the value derived from the bound
    spec's exact sweep law when that law is complete — the builder
    computes it, and a hand-asserted offset that disagrees with the law
    is rejected rather than archived. ``overlap_state`` records whether
    the component's tail enters the causal linear region.
    """

    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    spec_ref: AuthorityRef
    harmonic_order: int = Field(ge=2)
    expected_offset_s: float | None = None
    extraction_window: CadExtractionWindow | None = None
    valid_fundamental_low_hz: float | None = None
    valid_fundamental_high_hz: float | None = None
    overlap_state: HarmonicOverlapState = 'unknown'
    component_artifact_sha256: str | None = None
    measured_level_db: float | None = None
    noise_limited: bool = False
    declared_at_utc: str = Field(min_length=1)
    component_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_component(self) -> 'CadHarmonicImpulseComponent':
        _require_iso8601(
            self.declared_at_utc, 'component declared_at_utc'
        )
        if self.spec_ref.ref_sha256 is None:
            raise ValueError(
                'the deconvolution spec pin must carry its sha256'
            )
        for label, value in (
            ('expected_offset_s', self.expected_offset_s),
            ('valid_fundamental_low_hz', self.valid_fundamental_low_hz),
            ('valid_fundamental_high_hz', self.valid_fundamental_high_hz),
            ('measured_level_db', self.measured_level_db),
        ):
            if value is not None:
                _require_finite(value, f'harmonic component {label}')
        if (
            self.valid_fundamental_low_hz is not None
            and self.valid_fundamental_low_hz <= 0
        ):
            raise ValueError('fundamental band low edge must be positive')
        if (
            self.valid_fundamental_low_hz is not None
            and self.valid_fundamental_high_hz is not None
            and self.valid_fundamental_high_hz
            <= self.valid_fundamental_low_hz
        ):
            raise ValueError('fundamental band must be ascending')
        _require_sha(
            self.component_artifact_sha256,
            'component_artifact_sha256',
        )
        expected = _hash(self.identity_payload())
        if self.component_sha256 != expected:
            raise ValueError('harmonic component hash mismatch')
        if self.component_id != _semantic_id('harmn', expected):
            raise ValueError('component id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'spec_ref': self.spec_ref.model_dump(mode='json'),
            'harmonic_order': self.harmonic_order,
            'expected_offset_s': self.expected_offset_s,
            'extraction_window': (
                self.extraction_window.model_dump(mode='json')
                if self.extraction_window is not None
                else None
            ),
            'valid_fundamental_low_hz': self.valid_fundamental_low_hz,
            'valid_fundamental_high_hz': self.valid_fundamental_high_hz,
            'overlap_state': self.overlap_state,
            'component_artifact_sha256': self.component_artifact_sha256,
            'measured_level_db': self.measured_level_db,
            'noise_limited': self.noise_limited,
            'declared_at_utc': self.declared_at_utc,
        }


def harmonic_component_binding(
    component: CadHarmonicImpulseComponent,
) -> AuthorityRef:
    return AuthorityRef(
        kind='harmonic_impulse_component',
        ref_id=component.component_id,
        ref_sha256=component.component_sha256,
    )


# ---------------------------------------------------------------------------
# Recovered impulse response
# ---------------------------------------------------------------------------


class CadRecoveredImpulseResponse(BaseModel):
    """Sealed recovered-IR record.

    ``provenance_class`` decides which pins are mandatory:
    ``derived_full_provenance`` requires the deconvolution spec, the raw
    capture and the full-response artifact (the linear extract is a view,
    never a replacement); ``imported_final_only`` forbids all of them —
    unknown deconvolution history is recorded honestly via
    ``source_tool``/``source_metadata_json``.
    """

    model_config = ConfigDict(frozen=True)

    ir_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    provenance_class: IRProvenanceClass
    spec_ref: AuthorityRef | None = None
    raw_capture_ref: AuthorityRef | None = None
    full_response_sha256: str | None = None
    linear_extract_sha256: str | None = None
    linear_extraction_window: CadExtractionWindow | None = None
    harmonic_component_refs: tuple[AuthorityRef, ...] = ()
    transform_ref: AuthorityRef | None = None
    source_tool: str | None = None
    source_tool_version: str | None = None
    source_metadata_json: str = '{}'
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    ir_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_ir(self) -> 'CadRecoveredImpulseResponse':
        _require_iso8601(self.declared_at_utc, 'IR declared_at_utc')
        derived = self.provenance_class == 'derived_full_provenance'
        imported_final = self.provenance_class == 'imported_final_only'
        for label, ref in (
            ('spec_ref', self.spec_ref),
            ('raw_capture_ref', self.raw_capture_ref),
            ('transform_ref', self.transform_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256')
        for ref in self.harmonic_component_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'harmonic component pins must carry their sha256'
                )
        _require_sha(self.full_response_sha256, 'full_response_sha256')
        _require_sha(self.linear_extract_sha256, 'linear_extract_sha256')
        if derived:
            if self.spec_ref is None:
                raise ValueError(
                    'derived provenance requires the deconvolution spec pin'
                )
            if self.raw_capture_ref is None:
                raise ValueError(
                    'derived provenance requires the raw capture pin — '
                    'the full response is never authoritative without it'
                )
            if self.full_response_sha256 is None:
                raise ValueError(
                    'derived provenance requires the full-response '
                    'artifact digest — extracting the linear IR never '
                    'overwrites the deconvolution result'
                )
        if imported_final:
            if self.spec_ref is not None:
                raise ValueError(
                    'imported_final_only cannot carry a deconvolution '
                    'spec — record the unknown provenance honestly'
                )
            if self.harmonic_component_refs:
                raise ValueError(
                    'imported_final_only cannot carry harmonic components'
                )
            if self.source_tool is None:
                raise ValueError(
                    'an imported final IR must name its source tool — '
                    'provenance honesty requires at least that'
                )
        expected = _hash(self.identity_payload())
        if self.ir_sha256 != expected:
            raise ValueError('recovered IR hash mismatch')
        if self.ir_id != _semantic_id('recir', expected):
            raise ValueError('recovered IR id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'provenance_class': self.provenance_class,
            'spec_ref': (
                self.spec_ref.model_dump(mode='json')
                if self.spec_ref is not None
                else None
            ),
            'raw_capture_ref': (
                self.raw_capture_ref.model_dump(mode='json')
                if self.raw_capture_ref is not None
                else None
            ),
            'full_response_sha256': self.full_response_sha256,
            'linear_extract_sha256': self.linear_extract_sha256,
            'linear_extraction_window': (
                self.linear_extraction_window.model_dump(mode='json')
                if self.linear_extraction_window is not None
                else None
            ),
            'harmonic_component_refs': [
                ref.model_dump(mode='json')
                for ref in self.harmonic_component_refs
            ],
            'transform_ref': (
                self.transform_ref.model_dump(mode='json')
                if self.transform_ref is not None
                else None
            ),
            'source_tool': self.source_tool,
            'source_tool_version': self.source_tool_version,
            'source_metadata_json': self.source_metadata_json,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def recovered_ir_binding(
    ir: CadRecoveredImpulseResponse,
) -> AuthorityRef:
    return AuthorityRef(
        kind='recovered_impulse_response',
        ref_id=ir.ir_id,
        ref_sha256=ir.ir_sha256,
    )


# ---------------------------------------------------------------------------
# Linear-IR capability verdict
# ---------------------------------------------------------------------------


class CadLinearIRCapability(BaseModel):
    """Sealed fail-closed verdict: which uses of one recovered IR are
    physically eligible.

    ``contamination_state`` keeps the causal-contamination verdict
    explicit; ``clock_gate``/``chain_gate`` record how the #609 timebase
    and #695 chain assessments constrained this IR. Every metric is
    reported — an absent capability would silently read as "fine".
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    ir_ref: AuthorityRef
    contamination_state: IRContaminationState
    clock_gate: ClockGateState
    chain_gate: ChainGateState
    capabilities: tuple[tuple[IRMetricCapability, CapabilityState], ...]
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_capability(self) -> 'CadLinearIRCapability':
        _require_iso8601(
            self.evaluated_at_utc, 'capability evaluated_at_utc'
        )
        if self.ir_ref.ref_sha256 is None:
            raise ValueError('capability must pin the IR sha256')
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_IR_METRIC_CAPABILITIES):
            raise ValueError(
                'an IR capability verdict must report every metric — an '
                'omitted capability would silently read as valid'
            )
        if len(self.capabilities) != len(set(ALL_IR_METRIC_CAPABILITIES)):
            raise ValueError('duplicate capability entries')
        expected = _hash(self.identity_payload())
        if self.capability_sha256 != expected:
            raise ValueError('capability hash mismatch')
        if self.capability_id != _semantic_id('lircap', expected):
            raise ValueError('capability id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'ir_ref': self.ir_ref.model_dump(mode='json'),
            'contamination_state': self.contamination_state,
            'clock_gate': self.clock_gate,
            'chain_gate': self.chain_gate,
            'capabilities': [list(item) for item in self.capabilities],
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: IRMetricCapability
    ) -> CapabilityState:
        return dict(self.capabilities)[capability]


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


def build_deconvolution_spec(
    *,
    document_id: str,
    stimulus_ref: AuthorityRef,
    sweep_law: SweepLaw,
    sweep_f_start_hz: float | None = None,
    sweep_f_end_hz: float | None = None,
    sweep_duration_s: float | None = None,
    sweep_sample_rate_hz: float | None = None,
    algorithm: DeconvolutionAlgorithm,
    implementation_version: str | None = None,
    inverse_construction: InverseConstruction = 'unknown',
    normalization: str | None = None,
    fft_block_size: int | None = None,
    zero_padding: str | None = None,
    regularization: str | None = None,
    resampling_declaration: str | None = None,
    time_origin_convention: TimeOriginConvention = 'unknown',
    output_scale: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadSweepDeconvolutionSpec:
    """Seal a swept-sine deconvolution spec."""
    payload = dict(
        document_id=document_id,
        stimulus_ref=stimulus_ref,
        sweep_law=sweep_law,
        sweep_f_start_hz=sweep_f_start_hz,
        sweep_f_end_hz=sweep_f_end_hz,
        sweep_duration_s=sweep_duration_s,
        sweep_sample_rate_hz=sweep_sample_rate_hz,
        algorithm=algorithm,
        implementation_version=implementation_version,
        inverse_construction=inverse_construction,
        normalization=normalization,
        fft_block_size=fft_block_size,
        zero_padding=zero_padding,
        regularization=regularization,
        resampling_declaration=resampling_declaration,
        time_origin_convention=time_origin_convention,
        output_scale=output_scale,
        authority_version=SWEEP_DECONV_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadSweepDeconvolutionSpec, payload,
        'spec_id', 'spec_sha256', 'swspec',
    )


def build_harmonic_component(
    *,
    document_id: str,
    spec: CadSweepDeconvolutionSpec,
    harmonic_order: int,
    extraction_window: CadExtractionWindow | None = None,
    overlap_state: HarmonicOverlapState = 'unknown',
    component_artifact_sha256: str | None = None,
    measured_level_db: float | None = None,
    noise_limited: bool = False,
    declared_at_utc: str | None = None,
) -> CadHarmonicImpulseComponent:
    """Seal one harmonic-order component, deriving its expected time
    offset and valid fundamental band from the spec's exact sweep law."""
    offset = spec.harmonic_offset_s(harmonic_order)
    band = spec.harmonic_valid_band(harmonic_order)
    payload = dict(
        document_id=document_id,
        spec_ref=deconv_spec_binding(spec),
        harmonic_order=harmonic_order,
        expected_offset_s=offset,
        extraction_window=extraction_window,
        valid_fundamental_low_hz=(band[0] if band is not None else None),
        valid_fundamental_high_hz=(band[1] if band is not None else None),
        overlap_state=overlap_state,
        component_artifact_sha256=component_artifact_sha256,
        measured_level_db=measured_level_db,
        noise_limited=noise_limited,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadHarmonicImpulseComponent, payload,
        'component_id', 'component_sha256', 'harmn',
    )


def build_recovered_ir(
    *,
    document_id: str,
    provenance_class: IRProvenanceClass,
    spec_ref: AuthorityRef | CadSweepDeconvolutionSpec | None = None,
    raw_capture_ref: AuthorityRef | None = None,
    full_response_sha256: str | None = None,
    linear_extract_sha256: str | None = None,
    linear_extraction_window: CadExtractionWindow | None = None,
    harmonic_component_refs: (
        tuple[AuthorityRef | CadHarmonicImpulseComponent, ...]
        | list[AuthorityRef | CadHarmonicImpulseComponent]
    ) = (),
    transform_ref: AuthorityRef | None = None,
    source_tool: str | None = None,
    source_tool_version: str | None = None,
    source_metadata_json: str = '{}',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadRecoveredImpulseResponse:
    """Seal a recovered-IR record."""

    def _comp_ref(value):
        if isinstance(value, CadHarmonicImpulseComponent):
            return harmonic_component_binding(value)
        return value

    if isinstance(spec_ref, CadSweepDeconvolutionSpec):
        spec_ref = deconv_spec_binding(spec_ref)
    payload = dict(
        document_id=document_id,
        provenance_class=provenance_class,
        spec_ref=spec_ref,
        raw_capture_ref=raw_capture_ref,
        full_response_sha256=full_response_sha256,
        linear_extract_sha256=linear_extract_sha256,
        linear_extraction_window=linear_extraction_window,
        harmonic_component_refs=tuple(
            _comp_ref(ref) for ref in harmonic_component_refs
        ),
        transform_ref=transform_ref,
        source_tool=source_tool,
        source_tool_version=source_tool_version,
        source_metadata_json=source_metadata_json,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadRecoveredImpulseResponse, payload,
        'ir_id', 'ir_sha256', 'recir',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_linear_ir_capability(
    *,
    document_id: str,
    ir: CadRecoveredImpulseResponse,
    spec: CadSweepDeconvolutionSpec | None = None,
    harmonic_components: tuple[CadHarmonicImpulseComponent, ...]
    | list[CadHarmonicImpulseComponent] = (),
    clock_gate: ClockGateState = 'unassessed',
    chain_gate: ChainGateState = 'unassessed',
    evaluated_at_utc: str | None = None,
) -> CadLinearIRCapability:
    """Fail-closed capability verdict for one recovered IR.

    ``clock_gate`` is the #609-derived state (``synchronized`` only when a
    synchronous topology with a strong evidence basis carried it);
    ``chain_gate`` is the #695-derived state. Unassessed inputs never
    silently upgrade to "fine" — they hold capabilities at ``unknown``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[IRMetricCapability, CapabilityState] = {}

    # --- contamination verdict -------------------------------------------
    if ir.provenance_class == 'imported_final_only':
        contamination: IRContaminationState = 'insufficient_evidence'
        reasons.append(
            'imported final IR — sweep/deconvolution/harmonic provenance '
            'is UNKNOWN; only metadata-supported quantities may claim '
            'strength'
        )
    elif not harmonic_components:
        if spec is not None and spec.sweep_law == 'exponential':
            contamination = 'linear_ir_clean_within_declared_window'
        else:
            contamination = 'insufficient_evidence'
            reasons.append(
                'no harmonic components declared and the sweep law is '
                'not derivable — nonlinear contamination is unassessed'
            )
    else:
        states = {component.overlap_state for component in harmonic_components}
        if 'inseparable' in states:
            contamination = 'inseparable'
        elif 'contaminates_causal' in states:
            contamination = 'causal_nonlinear_contamination_risk'
        elif 'partial_overlap' in states:
            contamination = 'partial_overlap'
        elif states == {'separated'}:
            contamination = 'nonlinear_components_separated'
        else:
            contamination = 'insufficient_evidence'

    # --- noise / level limits ---------------------------------------------
    noise_limited_orders = any(
        component.noise_limited for component in harmonic_components
    )
    if noise_limited_orders:
        reasons.append(
            'low-level harmonic orders sit near the noise floor — they '
            'are noise-limited, not precise distortion evidence'
        )

    # --- metric gating ------------------------------------------------------
    if ir.provenance_class == 'imported_final_only':
        caps['fr_valid'] = 'limited'
        caps['direct_arrival_valid'] = 'unknown'
        caps['early_reflection_valid'] = 'unknown'
        caps['decay_metric_valid'] = 'unknown'
        caps['clarity_valid'] = 'unknown'
        caps['absolute_phase_valid'] = 'unknown'
    else:
        contaminated = contamination in (
            'partial_overlap',
            'causal_nonlinear_contamination_risk',
        )
        inseparable = contamination == 'inseparable'
        unassessed = contamination == 'insufficient_evidence'

        caps['fr_valid'] = (
            'invalid' if inseparable
            else 'limited' if contaminated or unassessed
            else 'valid'
        )
        caps['direct_arrival_valid'] = (
            'invalid' if inseparable
            else 'limited' if contaminated or unassessed
            else 'valid'
        )
        caps['early_reflection_valid'] = (
            'invalid' if inseparable or contaminated
            else 'unknown' if unassessed
            else 'valid'
        )
        caps['decay_metric_valid'] = (
            'invalid' if inseparable
            else 'limited' if contaminated
            else 'unknown' if unassessed
            else 'valid'
        )
        caps['clarity_valid'] = caps['early_reflection_valid']

    # --- clock gate ---------------------------------------------------------
    if clock_gate == 'unsynchronized_declared':
        caps['absolute_phase_valid'] = 'invalid'
        if caps.get('early_reflection_valid') == 'valid':
            caps['early_reflection_valid'] = 'limited'
        reasons.append(
            'declared asynchronous clocks cannot carry exact '
            'higher-harmonic or absolute-phase analysis'
        )
    elif clock_gate == 'synchronized':
        caps.setdefault('absolute_phase_valid', 'valid')
        if caps.get('absolute_phase_valid', 'unknown') == 'unknown':
            caps['absolute_phase_valid'] = 'valid'
    else:
        # unassessed / unknown — never an upgrade
        caps.setdefault('absolute_phase_valid', 'unknown')
        if caps.get('absolute_phase_valid') == 'valid':
            caps['absolute_phase_valid'] = 'limited'
        else:
            caps['absolute_phase_valid'] = 'unknown'
        if clock_gate == 'unassessed':
            reasons.append(
                'timebase capability unassessed — exact phase/harmonic '
                'claims stay limited'
            )

    # --- chain gate -----------------------------------------------------------
    if chain_gate == 'overload_observed':
        reasons.append(
            'acquisition-chain overload observed — harmonic components '
            'may be measurement-chain products, and the linear region '
            'itself is suspect'
        )
        for key in list(caps):
            if caps[key] == 'valid':
                caps[key] = 'limited'
            elif caps[key] == 'limited':
                caps[key] = 'invalid'
    elif chain_gate == 'overload_suspected':
        reasons.append(
            'acquisition-chain overload suspected — distortion-adjacent '
            'claims degrade'
        )
        for key in list(caps):
            if caps[key] == 'valid':
                caps[key] = 'limited'
    elif chain_gate in ('unassessed', 'unknown'):
        for key in ('early_reflection_valid', 'clarity_valid',
                    'decay_metric_valid'):
            if caps.get(key) == 'valid':
                caps[key] = 'limited'
        reasons.append(
            'measurement-chain linearity unassessed — nonlinear '
            'interpretation stays limited'
        )

    payload = dict(
        document_id=document_id,
        ir_ref=recovered_ir_binding(ir),
        contamination_state=contamination,
        clock_gate=clock_gate,
        chain_gate=chain_gate,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_IR_METRIC_CAPABILITIES
        ),
        reasons=tuple(reasons),
        evaluation_version=SWEEP_DECONV_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadLinearIRCapability, payload,
        'capability_id', 'capability_sha256', 'lircap',
    )


__all__ = [
    'ALL_IR_METRIC_CAPABILITIES',
    'CadExtractionWindow',
    'CadHarmonicImpulseComponent',
    'CadLinearIRCapability',
    'CadRecoveredImpulseResponse',
    'CadSweepDeconvolutionSpec',
    'CapabilityState',
    'ChainGateState',
    'ClockGateState',
    'DeconvolutionAlgorithm',
    'HarmonicOverlapState',
    'InverseConstruction',
    'IRContaminationState',
    'IRMetricCapability',
    'IRProvenanceClass',
    'SWEEP_DECONV_EVALUATION_VERSION',
    'SWEEP_DECONV_SCHEMA_VERSION',
    'SweepLaw',
    'TimeOriginConvention',
    'build_deconvolution_spec',
    'build_harmonic_component',
    'build_recovered_ir',
    'deconv_spec_binding',
    'evaluate_linear_ir_capability',
    'harmonic_component_binding',
    'recovered_ir_binding',
]
