"""DSP filter-realization qualification authority (#679, REV58-DSPDECAY).

Requesting or reading back the same nominal PEQ/FIR settings never proves
that the target device realizes the exact designed transfer function. UI/API
step rounding, vendor bandwidth/Q conventions, bilinear-transform variants,
sample-rate dependence, fixed-point coefficient quantization, arithmetic
roundoff/saturation, biquad cascade ordering, filter-count limits, FIR tap
truncation and hidden resampling all live between the nominal design and the
realized device transfer — and none of them are visible in a nominal readback.

This module makes design→realization fidelity a sealed, fail-closed
authority:

- :class:`CadDspRealizationProfile` — the declared device/DSP realization
  profile: device/firmware/engine identity, processing sample rate(s),
  filter banks with their parameter conventions and deployable grids
  (step/range per parameter), filter-count and FIR tap limits, coefficient
  format/word length *only when legitimately documented*, processing-order
  semantics, hidden-processing declarations and provider provenance.
  Unknown internals stay ``unknown`` — a marketing "64-bit DSP" label is
  never a coefficient-word-length claim.

- :class:`CadDspStageRecord` — one sealed stage of the closed-loop chain
  (``ideal_design`` → ``device_target_parameterization`` →
  ``encoded_artifact`` → ``requested_device_state`` →
  ``observed_readback_state`` → ``predicted_realized_transfer`` →
  ``measured_realized_transfer``). Stages are distinct records, hash-linked
  by parent refs; no stage silently overwrites another.

- :class:`CadDspParameterMapping` — the deterministic per-parameter mapping
  record (requested value → deployable value, rounding/clamping rule,
  device range, delta, reason, verdict). A required-but-unsupported setting
  is ``unsupported_rejected`` — never a silent clamp.

- :class:`CadDspRealizationQualification` — the fail-closed verdict binding
  design/profile/stage pins to the realization state, with nominal
  readback match and transfer realization kept as *separate* statuses:
  ``device_readback_match`` never promotes to
  ``transfer_realization_verified`` on its own.

Composition:

- #592 ``cad_device_snapshot`` — the observed/readback device state and
  firmware identity a stage record pins.
- FIR10 ``cad_fir_filter`` — ``FIRFilterArtifact`` is the coefficient-bearing
  artifact a stage pins; ``DeviceFIRCapabilityProfile`` covers tap limits.
- #1072 ``cad_camilladsp`` — the open target whose documented semantics can
  carry ``predicted_realized_transfer``.
- #670 playback clock/sample-rate state — sample rate is part of the
  realization identity here, not a global assumption.
- #572/#604 uncertainty — realization error contributes the separate
  ``dsp_*`` components; it is never folded into room-model residual.

Honesty rules baked into the models and the evaluator:

- Matching nominal readback is *state* evidence, not *transfer* evidence:
  a readback that echoes the requested UI parameters proves reporting, not
  internal coefficients, hidden resampling, guardrails or ordering.
- Every rounding/clamp is explicit and reproducible: a mapping row carries
  its rule, range and delta; unsupported requirements fail closed to
  ``incompatible``/``reoptimization_required``.
- Coefficient/word-length internals are ``unknown`` unless documented or
  measured — never inferred from marketing labels or audible behaviour.
- Coefficients generated for one sample rate are never reused at another
  unless the representation is declared sample-rate independent.
- The same filter set realized through a different processing order is a
  different realization claim under finite precision/headroom.

Literature / standards basis
----------------------------
- Bristow-Johnson, *The Equivalence of Various Methods of Computing Biquad
  Coefficients for Audio Parametric Equalizers*, AES Convention 97, paper
  3906 (1994): ``f0/gain/Q`` is an interface-level representation;
  reproducible realization needs the exact parameter convention, sample
  rate and coefficient-generation mapping.
- Agrawal et al., *Design of digital IIR filter: A research survey*,
  Applied Acoustics 172 (2021) 107669: coefficient quantization moves
  poles; high-Q / poles-near-unit-circle designs are particularly
  sensitive to word-length effects.
- Liu, Hirano & Nishiura, *Error analysis in fixed-point digital filters
  using sign-magnitude truncation*, J. Franklin Institute 308(1), 1979:
  input quantization, arithmetic roundoff and finite-precision
  coefficients are distinct error classes.
- *Estimating Word Lengths for Fixed-Point DSP Implementations Using
  Polynomial Chaos Expansions*, Electronics 14(2) 365 (2025): reduced
  coefficient precision can materially degrade implemented filter
  performance (implementation examples, not universal numbers).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


DSPREAL_AUTHORITY_SCHEMA_VERSION = 'dspreal-dsp-1'
DSPREAL_EVALUATION_VERSION = 'dspreal-eval-1'

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
# Taxonomies (#679)
# ---------------------------------------------------------------------------

DspStageKind = Literal[
    'ideal_design',
    'device_target_parameterization',
    'encoded_artifact',
    'requested_device_state',
    'observed_readback_state',
    'predicted_realized_transfer',
    'measured_realized_transfer',
]

ALL_DSP_STAGES: tuple[DspStageKind, ...] = (
    'ideal_design',
    'device_target_parameterization',
    'encoded_artifact',
    'requested_device_state',
    'observed_readback_state',
    'predicted_realized_transfer',
    'measured_realized_transfer',
)

DspFilterFamily = Literal[
    'peq_biquad',
    'fir_convolution',
    'shelf',
    'crossover',
    'gain_delay',
    'mixed',
    'proprietary_hidden',
    'unknown',
]

DspParameterKind = Literal[
    'frequency_hz',
    'gain_db',
    'q_factor',
    'bandwidth_oct',
    'shelf_slope',
    'fir_tap_count',
    'channel_trim_db',
    'delay',
    'other',
]

DspParameterConvention = Literal[
    'q_factor',
    'octave_bandwidth',
    'hz_bandwidth',
    'shelf_slope_db_per_oct',
    'bristow_johnson_q',
    'vendor_defined',
    'unknown',
]

CoefficientFormat = Literal[
    'float32',
    'float64',
    'fixed_point_documented',
    'fixed_point_undocumented',
    'proprietary_hidden',
    'unknown',
]

CoefficientExposure = Literal[
    'exact_coefficients_exposed',
    'quantized_coefficients_exposed',
    'nominal_parameters_only',
    'hidden',
    'unknown',
]

RoundingRule = Literal[
    'nearest_step',
    'floor_step',
    'ceil_step',
    'clamp_to_range',
    'reject_out_of_range',
    'exact_no_rounding',
    'vendor_hidden',
    'unknown',
]

MappingVerdict = Literal[
    'exact',
    'rounded_within_step',
    'clamped_to_range',
    'unsupported_rejected',
    'unmappable_convention',
    'unknown',
]

DspRealizationState = Literal[
    'realized_within_declared_model',
    'realized_with_declared_approximation',
    'nominal_state_match_only',
    'realization_model_limited',
    'incompatible',
    'reoptimization_required',
    'unqualified',
]

TransferVerificationState = Literal[
    'transfer_realization_verified',
    'nominal_state_match_only',
    'transfer_mismatch_detected',
    'transfer_unverified',
]

ProcessingOrderState = Literal['declared', 'partially_declared', 'unknown']

#: Distinct DSP realization error contributions surfaced to #572/#604 —
#: never folded into a room-model residual.
DspErrorComponent = Literal[
    'dsp_parameter_rounding',
    'dsp_coefficient_quantization',
    'dsp_implementation_unknown',
    'sample_rate_mapping',
    'device_hidden_processing',
]

ALL_DSP_ERROR_COMPONENTS: tuple[DspErrorComponent, ...] = (
    'dsp_parameter_rounding',
    'dsp_coefficient_quantization',
    'dsp_implementation_unknown',
    'sample_rate_mapping',
    'device_hidden_processing',
)


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadDspParameterGrid(BaseModel):
    """The deployable grid for one parameter on one device/bank.

    ``step`` is the device-enforced resolution (UI/API step or coefficient
    domain quantum); ``minimum``/``maximum`` are the deployable range. A
    parameter with no declared step is ``exact_no_rounding`` only when the
    convention says so — otherwise its rounding rule is ``unknown`` and the
    evaluator never assumes continuous acceptance.
    """

    model_config = ConfigDict(frozen=True)

    parameter: DspParameterKind
    unit: str = Field(min_length=1)
    step: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    convention: DspParameterConvention = 'unknown'
    rounding_rule: RoundingRule = 'unknown'

    @model_validator(mode='after')
    def valid_grid(self) -> 'CadDspParameterGrid':
        for label, value in (
            ('step', self.step),
            ('minimum', self.minimum),
            ('maximum', self.maximum),
        ):
            if value is not None:
                _require_finite(value, f'parameter grid {label}')
        if self.step is not None and self.step <= 0:
            raise ValueError('parameter grid step must be positive')
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum < self.minimum
        ):
            raise ValueError('parameter grid range must be ascending')
        return self

    def map_value(self, requested: float) -> 'CadDspGridMapResult':
        """Deterministic request→deployable mapping for this grid.

        Out-of-range under ``reject_out_of_range`` (or an undeclared
        clamping rule) rejects — a silent clamp is never produced. An
        undeclared step reports the request verbatim and marks the mapping
        ``unknown`` rather than claiming exactness.
        """
        _require_finite(requested, 'requested parameter value')
        in_range = True
        if self.minimum is not None and requested < self.minimum:
            in_range = False
        if self.maximum is not None and requested > self.maximum:
            in_range = False
        if not in_range:
            if self.rounding_rule == 'clamp_to_range':
                deployable = min(
                    max(requested, self.minimum),
                    self.maximum,
                )
                return CadDspGridMapResult(
                    requested=requested,
                    deployable=deployable,
                    delta=deployable - requested,
                    rule='clamp_to_range',
                    verdict='clamped_to_range',
                    reason='request outside the declared deployable range',
                )
            return CadDspGridMapResult(
                requested=requested,
                deployable=None,
                delta=None,
                rule=self.rounding_rule,
                verdict='unsupported_rejected',
                reason=(
                    'request outside the deployable range and the profile '
                    'declares no clamping rule'
                ),
            )
        if self.step is None:
            return CadDspGridMapResult(
                requested=requested,
                deployable=requested,
                delta=0.0,
                rule=self.rounding_rule,
                verdict=(
                    'exact'
                    if self.rounding_rule == 'exact_no_rounding'
                    else 'unknown'
                ),
                reason=(
                    'no declared step — deployable value cannot be proven '
                    'exact'
                    if self.rounding_rule != 'exact_no_rounding'
                    else 'exact representation declared'
                ),
            )
        if self.rounding_rule in ('reject_out_of_range', 'vendor_hidden'):
            # In-range but the rounding behaviour itself is undocumented or
            # declared reject-only: never fabricate a rounded value.
            return CadDspGridMapResult(
                requested=requested,
                deployable=None,
                delta=None,
                rule=self.rounding_rule,
                verdict='unknown',
                reason=(
                    'in-range but the rounding/quantization rule is not '
                    'reproducible'
                ),
            )
        base = self.minimum if self.minimum is not None else 0.0
        steps = (requested - base) / self.step
        if self.rounding_rule == 'floor_step':
            from math import floor

            deployable = base + floor(steps + 1e-12) * self.step
        elif self.rounding_rule == 'ceil_step':
            from math import ceil

            deployable = base + ceil(steps - 1e-12) * self.step
        else:  # nearest_step / unknown → document the honest nearest map
            deployable = base + round(steps) * self.step
        deployable = float(deployable)
        delta = deployable - requested
        return CadDspGridMapResult(
            requested=requested,
            deployable=deployable,
            delta=delta,
            rule=self.rounding_rule,
            verdict='exact' if delta == 0.0 else 'rounded_within_step',
            reason='',
        )


class CadDspGridMapResult(BaseModel):
    """One deterministic grid mapping outcome (computed, not stored)."""

    model_config = ConfigDict(frozen=True)

    requested: float
    deployable: float | None
    delta: float | None
    rule: RoundingRule
    verdict: MappingVerdict
    reason: str = ''


class CadDspFilterBank(BaseModel):
    """One declared filter bank on the device.

    ``max_filter_count``/``max_fir_taps`` are hard deployable limits;
    ``parameter_convention`` is the bank's meaning of Q/bandwidth/shelf
    slope — two devices both accepting ``Q=1.0`` implement identical
    transfers only when the conventions are declared compatible.
    """

    model_config = ConfigDict(frozen=True)

    bank_label: str = Field(min_length=1)
    family: DspFilterFamily
    max_filter_count: int | None = None
    max_fir_taps: int | None = None
    parameter_convention: DspParameterConvention = 'unknown'
    parameter_grids: tuple[CadDspParameterGrid, ...] = ()
    processing_position: str | None = None
    coefficient_exposure: CoefficientExposure = 'unknown'
    coefficient_format: CoefficientFormat = 'unknown'
    notes: str | None = None

    @model_validator(mode='after')
    def valid_bank(self) -> 'CadDspFilterBank':
        if self.max_filter_count is not None and (
            self.max_filter_count < 1
        ):
            raise ValueError('max_filter_count must be positive')
        if self.max_fir_taps is not None and self.max_fir_taps < 1:
            raise ValueError('max_fir_taps must be positive')
        return self


class CadDspParameterMappingEntry(BaseModel):
    """One parameter's request→deployable record inside a mapping."""

    model_config = ConfigDict(frozen=True)

    parameter: DspParameterKind
    requested_value: float
    deployable_value: float | None
    delta: float | None
    rule: RoundingRule
    verdict: MappingVerdict
    reason: str = ''

    @model_validator(mode='after')
    def valid_entry(self) -> 'CadDspParameterMappingEntry':
        _require_finite(self.requested_value, 'mapping requested_value')
        if self.deployable_value is not None:
            _require_finite(
                self.deployable_value, 'mapping deployable_value'
            )
        if self.delta is not None:
            _require_finite(self.delta, 'mapping delta')
        if self.verdict == 'unsupported_rejected' and (
            self.deployable_value is not None
        ):
            raise ValueError(
                'a rejected mapping must not carry a deployable value'
            )
        if self.verdict in ('rounded_within_step', 'clamped_to_range') and (
            self.deployable_value is None
        ):
            raise ValueError(
                'a rounded/clamped mapping requires the deployable value'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadDspRealizationProfile(BaseModel):
    """Sealed device/DSP realization profile (#679 §2).

    Everything about the device's deployable parameter space that is
    legitimately known: identity (device/firmware/engine), processing
    sample rate(s), filter banks with conventions and grids, processing
    order, hidden-processing declarations and where the knowledge came
    from. ``sample_rates_hz`` is the set of processing rates the profile
    covers — a coefficient set generated for 48 kHz is never claimed to
    realize the same transfer at 96 kHz.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    device_identity: str = Field(min_length=1)
    firmware_identity: str | None = None
    dsp_engine_label: str | None = None
    sample_rates_hz: tuple[float, ...]
    filter_banks: tuple[CadDspFilterBank, ...]
    processing_order_state: ProcessingOrderState = 'unknown'
    processing_order: tuple[str, ...] = ()
    hidden_processing: tuple[str, ...] = ()
    internal_resampling: Literal[
        'none_declared', 'declared', 'suspected', 'unknown'
    ] = 'unknown'
    provider_provenance: Literal[
        'vendor_documentation',
        'open_source_inspection',
        'measured_observation',
        'assumed',
        'unknown',
    ] = 'unknown'
    provenance_source: str | None = None
    device_snapshot_ref: AuthorityRef | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadDspRealizationProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if not self.sample_rates_hz:
            raise ValueError(
                'a realization profile requires at least one declared '
                'processing sample rate — the rate domain is part of the '
                'realization identity'
            )
        for rate in self.sample_rates_hz:
            _require_finite(rate, 'sample_rates_hz')
            if rate <= 0:
                raise ValueError('sample rate must be positive')
        if not self.filter_banks:
            raise ValueError(
                'a realization profile requires at least one declared '
                'filter bank — even an "opaque proprietary bank" is a '
                'declaration'
            )
        if self.device_snapshot_ref is not None and (
            self.device_snapshot_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #592 device-snapshot pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('dsp realization profile hash mismatch')
        if self.profile_id != _semantic_id('dsppro', expected):
            raise ValueError(
                'dsp realization profile id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'device_identity': self.device_identity,
            'firmware_identity': self.firmware_identity,
            'dsp_engine_label': self.dsp_engine_label,
            'sample_rates_hz': list(self.sample_rates_hz),
            'filter_banks': [
                b.model_dump(mode='json') for b in self.filter_banks
            ],
            'processing_order_state': self.processing_order_state,
            'processing_order': list(self.processing_order),
            'hidden_processing': list(self.hidden_processing),
            'internal_resampling': self.internal_resampling,
            'provider_provenance': self.provider_provenance,
            'provenance_source': self.provenance_source,
            'device_snapshot_ref': (
                self.device_snapshot_ref.model_dump(mode='json')
                if self.device_snapshot_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def bank_for(self, bank_label: str) -> CadDspFilterBank | None:
        for bank in self.filter_banks:
            if bank.bank_label == bank_label:
                return bank
        return None


def dsp_profile_binding(
    profile: CadDspRealizationProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='dsp_realization_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class CadDspStageRecord(BaseModel):
    """One sealed stage of the design→realization chain (#679 §1).

    ``stage_kind`` is typed; ``state_summary_json`` carries the stage's
    canonical content summary (e.g. normalized PEQ tuples, coefficient set
    hash, measured-response dataset pin) — the record pins *what the state
    actually was*, not what it was supposed to be. ``parent_stage_ref``
    hash-links the stage into the chain; ``artifact_ref`` pins a
    coefficient-bearing artifact (e.g. an ``FIRFilterArtifact``) when one
    exists. An observed-readback stage pins the #592 snapshot it was taken
    from via ``device_snapshot_ref``.
    """

    model_config = ConfigDict(frozen=True)

    stage_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stage_kind: DspStageKind
    bank_label: str | None = None
    design_ref: AuthorityRef | None = None
    parent_stage_ref: AuthorityRef | None = None
    artifact_ref: AuthorityRef | None = None
    device_snapshot_ref: AuthorityRef | None = None
    active_sample_rate_hz: float | None = None
    parameter_convention: DspParameterConvention = 'unknown'
    state_summary_json: str = '{}'
    state_content_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    stage_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_stage_record(self) -> 'CadDspStageRecord':
        _require_iso8601(self.declared_at_utc, 'stage declared_at_utc')
        if self.active_sample_rate_hz is not None:
            _require_finite(
                self.active_sample_rate_hz, 'active_sample_rate_hz'
            )
            if self.active_sample_rate_hz <= 0:
                raise ValueError('active sample rate must be positive')
        for label, ref in (
            ('design_ref', self.design_ref),
            ('parent_stage_ref', self.parent_stage_ref),
            ('artifact_ref', self.artifact_ref),
            ('device_snapshot_ref', self.device_snapshot_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.stage_kind == 'observed_readback_state' and (
            self.state_content_sha256 is None
            and self.device_snapshot_ref is None
        ):
            raise ValueError(
                'an observed readback stage requires evidence — a content '
                'digest or the #592 snapshot it was read back from'
            )
        expected = _hash(self.identity_payload())
        if self.stage_sha256 != expected:
            raise ValueError('dsp stage record hash mismatch')
        if self.stage_id != _semantic_id('dspstg', expected):
            raise ValueError(
                'dsp stage record id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stage_kind': self.stage_kind,
            'bank_label': self.bank_label,
            'design_ref': (
                self.design_ref.model_dump(mode='json')
                if self.design_ref is not None
                else None
            ),
            'parent_stage_ref': (
                self.parent_stage_ref.model_dump(mode='json')
                if self.parent_stage_ref is not None
                else None
            ),
            'artifact_ref': (
                self.artifact_ref.model_dump(mode='json')
                if self.artifact_ref is not None
                else None
            ),
            'device_snapshot_ref': (
                self.device_snapshot_ref.model_dump(mode='json')
                if self.device_snapshot_ref is not None
                else None
            ),
            'active_sample_rate_hz': self.active_sample_rate_hz,
            'parameter_convention': self.parameter_convention,
            'state_summary_json': self.state_summary_json,
            'state_content_sha256': self.state_content_sha256,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def dsp_stage_binding(stage: CadDspStageRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='dsp_stage_record',
        ref_id=stage.stage_id,
        ref_sha256=stage.stage_sha256,
    )


class CadDspParameterMapping(BaseModel):
    """Sealed per-export deterministic parameter mapping (#679 §5).

    One record per exported parameterization: the bank it targets, the
    ordered per-parameter entries (requested → deployable + rule + delta +
    verdict) and the requested/active sample rate. ``state`` summarizes the
    whole mapping: any ``unsupported_rejected`` entry makes the mapping a
    rejection, never a partial silent success.
    """

    model_config = ConfigDict(frozen=True)

    mapping_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    bank_label: str = Field(min_length=1)
    requested_sample_rate_hz: float | None = None
    entries: tuple[CadDspParameterMappingEntry, ...]
    state: Literal[
        'exact',
        'mapped_with_declared_deltas',
        'partially_unsupported',
        'unsupported',
    ]
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    mapping_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_mapping(self) -> 'CadDspParameterMapping':
        _require_iso8601(self.declared_at_utc, 'mapping declared_at_utc')
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('mapping must pin the profile sha256')
        if not self.entries:
            raise ValueError(
                'a parameter mapping requires at least one entry'
            )
        if self.requested_sample_rate_hz is not None:
            _require_finite(
                self.requested_sample_rate_hz,
                'requested_sample_rate_hz',
            )
            if self.requested_sample_rate_hz <= 0:
                raise ValueError('requested sample rate must be positive')
        rejected = any(
            e.verdict == 'unsupported_rejected' for e in self.entries
        )
        undeployable = any(
            e.verdict in (
                'unsupported_rejected', 'unmappable_convention', 'unknown'
            )
            for e in self.entries
        )
        if self.state == 'exact' and undeployable:
            raise ValueError(
                'an exact mapping cannot contain undeployable entries'
            )
        if self.state == 'unsupported' and not undeployable:
            raise ValueError(
                'an unsupported mapping requires a rejected/unmappable/'
                'unknown entry'
            )
        expected = _hash(self.identity_payload())
        if self.mapping_sha256 != expected:
            raise ValueError('dsp parameter mapping hash mismatch')
        if self.mapping_id != _semantic_id('dspmap', expected):
            raise ValueError(
                'dsp parameter mapping id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'bank_label': self.bank_label,
            'requested_sample_rate_hz': self.requested_sample_rate_hz,
            'entries': [
                e.model_dump(mode='json') for e in self.entries
            ],
            'state': self.state,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def dsp_mapping_binding(
    mapping: CadDspParameterMapping,
) -> AuthorityRef:
    return AuthorityRef(
        kind='dsp_parameter_mapping',
        ref_id=mapping.mapping_id,
        ref_sha256=mapping.mapping_sha256,
    )


class CadDspRealizationQualification(BaseModel):
    """Sealed fail-closed realization verdict (#679 goal).

    Binds the design ref, the realization profile, the stage-chain pins
    and the parameter mapping to a realization state. ``readback_status``
    and ``transfer_verification`` are independent: matching nominal
    readback is device-state evidence and never auto-promotes to transfer
    verification — that requires a ``predicted_realized_transfer`` model
    comparison or a ``measured_realized_transfer`` stage.
    ``error_components`` declares which DSP-side error contributions are
    active for the #572/#604 uncertainty ledger.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    design_ref: AuthorityRef
    requested_stage_ref: AuthorityRef | None = None
    readback_stage_ref: AuthorityRef | None = None
    predicted_stage_ref: AuthorityRef | None = None
    measured_stage_ref: AuthorityRef | None = None
    mapping_ref: AuthorityRef | None = None
    active_sample_rate_hz: float | None = None
    state: DspRealizationState
    readback_status: Literal[
        'device_readback_match',
        'device_readback_mismatch',
        'readback_unavailable',
        'unassessed',
    ] = 'unassessed'
    transfer_verification: TransferVerificationState = (
        'transfer_unverified'
    )
    error_components: tuple[tuple[DspErrorComponent, str], ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadDspRealizationQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        for label, ref in (
            ('profile_ref', self.profile_ref),
            ('design_ref', self.design_ref),
            ('requested_stage_ref', self.requested_stage_ref),
            ('readback_stage_ref', self.readback_stage_ref),
            ('predicted_stage_ref', self.predicted_stage_ref),
            ('measured_stage_ref', self.measured_stage_ref),
            ('mapping_ref', self.mapping_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.active_sample_rate_hz is not None:
            _require_finite(
                self.active_sample_rate_hz, 'active_sample_rate_hz'
            )
            if self.active_sample_rate_hz <= 0:
                raise ValueError('active sample rate must be positive')
        if self.transfer_verification == 'transfer_realization_verified' and (
            self.predicted_stage_ref is None
            and self.measured_stage_ref is None
        ):
            raise ValueError(
                'transfer_realization_verified requires a predicted or '
                'measured realized-transfer stage — nominal readback '
                'match is not transfer proof (#679 §10)'
            )
        covered = {component for component, _ in self.error_components}
        if len(covered) != len(self.error_components):
            raise ValueError('duplicate error-component entries')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('dsp realization qualification hash mismatch')
        if self.qualification_id != _semantic_id('dspqual', expected):
            raise ValueError(
                'dsp realization qualification id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'design_ref': self.design_ref.model_dump(mode='json'),
            'requested_stage_ref': (
                self.requested_stage_ref.model_dump(mode='json')
                if self.requested_stage_ref is not None
                else None
            ),
            'readback_stage_ref': (
                self.readback_stage_ref.model_dump(mode='json')
                if self.readback_stage_ref is not None
                else None
            ),
            'predicted_stage_ref': (
                self.predicted_stage_ref.model_dump(mode='json')
                if self.predicted_stage_ref is not None
                else None
            ),
            'measured_stage_ref': (
                self.measured_stage_ref.model_dump(mode='json')
                if self.measured_stage_ref is not None
                else None
            ),
            'mapping_ref': (
                self.mapping_ref.model_dump(mode='json')
                if self.mapping_ref is not None
                else None
            ),
            'active_sample_rate_hz': self.active_sample_rate_hz,
            'state': self.state,
            'readback_status': self.readback_status,
            'transfer_verification': self.transfer_verification,
            'error_components': [
                list(item) for item in self.error_components
            ],
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def error_component_state(
        self, component: DspErrorComponent
    ) -> str | None:
        return dict(self.error_components).get(component)


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


def build_dsp_realization_profile(
    *,
    document_id: str,
    device_identity: str,
    sample_rates_hz: tuple[float, ...] | list[float],
    filter_banks: tuple[CadDspFilterBank, ...] | list[CadDspFilterBank],
    firmware_identity: str | None = None,
    dsp_engine_label: str | None = None,
    processing_order_state: ProcessingOrderState = 'unknown',
    processing_order: tuple[str, ...] | list[str] = (),
    hidden_processing: tuple[str, ...] | list[str] = (),
    internal_resampling: Literal[
        'none_declared', 'declared', 'suspected', 'unknown'
    ] = 'unknown',
    provider_provenance: Literal[
        'vendor_documentation',
        'open_source_inspection',
        'measured_observation',
        'assumed',
        'unknown',
    ] = 'unknown',
    provenance_source: str | None = None,
    device_snapshot_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDspRealizationProfile:
    """Seal a DSP realization profile."""
    payload = dict(
        document_id=document_id,
        device_identity=device_identity,
        firmware_identity=firmware_identity,
        dsp_engine_label=dsp_engine_label,
        sample_rates_hz=tuple(sample_rates_hz),
        filter_banks=tuple(filter_banks),
        processing_order_state=processing_order_state,
        processing_order=tuple(processing_order),
        hidden_processing=tuple(hidden_processing),
        internal_resampling=internal_resampling,
        provider_provenance=provider_provenance,
        provenance_source=provenance_source,
        device_snapshot_ref=device_snapshot_ref,
        authority_version=DSPREAL_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDspRealizationProfile, payload,
        'profile_id', 'profile_sha256', 'dsppro',
    )


def build_dsp_stage_record(
    *,
    document_id: str,
    stage_kind: DspStageKind,
    bank_label: str | None = None,
    design_ref: AuthorityRef | None = None,
    parent_stage_ref: AuthorityRef | CadDspStageRecord | None = None,
    artifact_ref: AuthorityRef | None = None,
    device_snapshot_ref: AuthorityRef | None = None,
    active_sample_rate_hz: float | None = None,
    parameter_convention: DspParameterConvention = 'unknown',
    state_summary_json: str = '{}',
    state_content_sha256: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDspStageRecord:
    """Seal one design→realization stage record."""
    if isinstance(parent_stage_ref, CadDspStageRecord):
        parent_stage_ref = dsp_stage_binding(parent_stage_ref)
    payload = dict(
        document_id=document_id,
        stage_kind=stage_kind,
        bank_label=bank_label,
        design_ref=design_ref,
        parent_stage_ref=parent_stage_ref,
        artifact_ref=artifact_ref,
        device_snapshot_ref=device_snapshot_ref,
        active_sample_rate_hz=active_sample_rate_hz,
        parameter_convention=parameter_convention,
        state_summary_json=state_summary_json,
        state_content_sha256=state_content_sha256,
        authority_version=DSPREAL_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDspStageRecord, payload,
        'stage_id', 'stage_sha256', 'dspstg',
    )


def build_dsp_parameter_mapping(
    *,
    document_id: str,
    profile: CadDspRealizationProfile,
    bank_label: str,
    requests: tuple[tuple[DspParameterKind, float], ...]
    | list[tuple[DspParameterKind, float]],
    requested_sample_rate_hz: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadDspParameterMapping:
    """Deterministically map requested parameters onto the device grid.

    Each ``(parameter, value)`` request is mapped through the bank's
    declared grid; a parameter with no declared grid produces an
    ``unknown`` entry (never a fabricated exact map). Any
    ``unsupported_rejected`` entry marks the whole mapping
    ``partially_unsupported``/``unsupported`` — the caller decides
    INCOMPATIBLE vs REOPTIMIZATION_REQUIRED downstream, but the mapping
    itself never reports success.
    """
    bank = profile.bank_for(bank_label)
    entries: list[CadDspParameterMappingEntry] = []
    for parameter, value in requests:
        _require_finite(value, f'requested {parameter}')
        grid = None
        if bank is not None:
            for candidate in bank.parameter_grids:
                if candidate.parameter == parameter:
                    grid = candidate
                    break
        if bank is None:
            entries.append(
                CadDspParameterMappingEntry(
                    parameter=parameter,
                    requested_value=value,
                    deployable_value=None,
                    delta=None,
                    rule='unknown',
                    verdict='unsupported_rejected',
                    reason='no such filter bank in the realization profile',
                )
            )
            continue
        if grid is None:
            entries.append(
                CadDspParameterMappingEntry(
                    parameter=parameter,
                    requested_value=value,
                    deployable_value=None,
                    delta=None,
                    rule='unknown',
                    verdict='unmappable_convention',
                    reason=(
                        'the bank declares no grid for this parameter — '
                        'the device mapping is undocumented'
                    ),
                )
            )
            continue
        result = grid.map_value(value)
        entries.append(
            CadDspParameterMappingEntry(
                parameter=parameter,
                requested_value=result.requested,
                deployable_value=result.deployable,
                delta=result.delta,
                rule=result.rule,
                verdict=result.verdict,
                reason=result.reason,
            )
        )
    verdicts = {e.verdict for e in entries}
    deployable_verdicts = {
        'exact', 'rounded_within_step', 'clamped_to_range'
    }
    if verdicts <= {'exact'}:
        state = 'exact'
    elif verdicts <= deployable_verdicts:
        state = 'mapped_with_declared_deltas'
    else:
        # Any rejected / unmappable / unknown-rule entry makes the
        # mapping a failure state — never a partial silent success.
        state = (
            'partially_unsupported'
            if verdicts & deployable_verdicts
            else 'unsupported'
        )
    payload = dict(
        document_id=document_id,
        profile_ref=dsp_profile_binding(profile),
        bank_label=bank_label,
        requested_sample_rate_hz=requested_sample_rate_hz,
        entries=tuple(entries),
        state=state,
        authority_version=DSPREAL_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadDspParameterMapping, payload,
        'mapping_id', 'mapping_sha256', 'dspmap',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_dsp_realization(
    *,
    document_id: str,
    profile: CadDspRealizationProfile,
    design_ref: AuthorityRef,
    requested_stage: CadDspStageRecord | None = None,
    readback_stage: CadDspStageRecord | None = None,
    predicted_stage: CadDspStageRecord | None = None,
    measured_stage: CadDspStageRecord | None = None,
    mapping: CadDspParameterMapping | None = None,
    evaluated_at_utc: str | None = None,
) -> CadDspRealizationQualification:
    """Fail-closed DSP realization verdict.

    Derivation order (#679): unsupported/clamped device requirements beat
    approximation claims; a nominal readback match reports
    ``nominal_state_match_only`` and never transfer verification; an
    undocumented parameter mapping or coefficient domain degrades the
    verdict to ``realization_model_limited`` rather than assuming ideal
    biquads. Sample-rate mismatch between the active request and the
    profile's declared processing rates fails closed to ``incompatible``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    if design_ref.ref_sha256 is None:
        raise ValueError('design_ref must carry its sha256 pin')
    reasons: list[str] = []
    errors: dict[DspErrorComponent, str] = {}

    # --- sample-rate domain ------------------------------------------------
    active_rate = None
    for stage in (requested_stage, readback_stage, predicted_stage,
                  measured_stage):
        if stage is not None and stage.active_sample_rate_hz is not None:
            active_rate = stage.active_sample_rate_hz
            break
    rate_mismatch = (
        active_rate is not None
        and active_rate not in profile.sample_rates_hz
    )
    if rate_mismatch:
        errors['sample_rate_mapping'] = (
            f'active {active_rate} Hz is outside the profile processing '
            'rates — coefficients generated for another rate are not the '
            'same realization'
        )
    elif active_rate is None:
        errors['sample_rate_mapping'] = (
            'no stage declares its processing sample rate'
        )

    # --- mapping verdict ---------------------------------------------------
    mapping_state = mapping.state if mapping is not None else None
    rejected = (
        mapping is not None
        and mapping_state in ('unsupported', 'partially_unsupported')
    )
    if rejected:
        errors['dsp_parameter_rounding'] = (
            'one or more required parameters are outside the deployable '
            'space — reoptimize on the deployable grid instead of '
            'clamping'
        )
    elif mapping is not None and any(
        e.verdict == 'unmappable_convention' for e in mapping.entries
    ):
        errors['dsp_implementation_unknown'] = (
            'one or more parameters have no declared device grid — the '
            'convention mapping is undocumented'
        )
    elif mapping is not None and any(
        e.verdict == 'clamped_to_range' for e in mapping.entries
    ):
        errors['dsp_parameter_rounding'] = (
            'one or more parameters were clamped to the device range — '
            'the deployed state is not the requested state'
        )
    elif mapping is not None and any(
        e.verdict == 'rounded_within_step' for e in mapping.entries
    ):
        errors['dsp_parameter_rounding'] = (
            'parameters rounded onto the declared device grid'
        )

    # --- coefficient/format unknowns ---------------------------------------
    exposures = {b.coefficient_exposure for b in profile.filter_banks}
    formats = {b.coefficient_format for b in profile.filter_banks}
    if exposures & {'proprietary_hidden', 'hidden', 'unknown'} or (
        formats & {'proprietary_hidden', 'fixed_point_undocumented',
                   'unknown'}
    ):
        errors['dsp_coefficient_quantization'] = (
            'coefficient format/word length is undocumented — '
            'quantization error is bounded only by declaration, not '
            'evidence'
        )
    if (
        profile.internal_resampling != 'none_declared'
        or profile.hidden_processing
        or profile.processing_order_state != 'declared'
    ):
        errors['device_hidden_processing'] = (
            'internal resampling / hidden processing / undeclared order '
            'remain possible device-side effects'
        )

    # --- readback vs transfer ----------------------------------------------
    if readback_stage is None:
        readback_status = 'readback_unavailable'
    elif (
        requested_stage is not None
        and readback_stage.state_summary_json
        == requested_stage.state_summary_json
    ):
        readback_status = 'device_readback_match'
    elif requested_stage is not None:
        readback_status = 'device_readback_mismatch'
        reasons.append(
            'readback state diverges from the requested state — the '
            'device is not reporting the requested parameterization'
        )
    else:
        readback_status = 'unassessed'

    if predicted_stage is not None or measured_stage is not None:
        transfer_verification = 'transfer_realization_verified'
    elif readback_status == 'device_readback_match':
        transfer_verification = 'nominal_state_match_only'
    elif readback_status == 'device_readback_mismatch':
        transfer_verification = 'transfer_mismatch_detected'
    else:
        transfer_verification = 'transfer_unverified'

    # --- state ---------------------------------------------------------------
    if rate_mismatch:
        state = 'incompatible'
        reasons.append(
            'the active processing rate is not a rate this realization '
            'profile covers'
        )
    elif rejected:
        state = 'reoptimization_required'
        reasons.append(
            'the design requires undeployable parameters — reoptimize on '
            'the deployable grid'
        )
    elif measured_stage is not None:
        state = 'realized_within_declared_model'
    elif predicted_stage is not None:
        state = (
            'realized_with_declared_approximation'
            if errors
            else 'realized_within_declared_model'
        )
    elif errors.get('dsp_implementation_unknown') or (
        'dsp_coefficient_quantization' in errors
        and 'device_hidden_processing' in errors
    ):
        state = 'realization_model_limited'
        reasons.append(
            'the device internals are undocumented — nominal deployment '
            'is recorded but the realized transfer is model-limited until '
            'measured'
        )
    elif mapping_state == 'exact':
        state = 'realized_within_declared_model'
    elif mapping_state in (None, 'mapped_with_declared_deltas'):
        state = 'realized_with_declared_approximation'
        if mapping_state is None:
            reasons.append(
                'no parameter mapping record — rounding/clamping was '
                'never audited'
            )
    else:
        state = 'unqualified'

    if not reasons:
        reasons.append(state)
    payload = dict(
        document_id=document_id,
        profile_ref=dsp_profile_binding(profile),
        design_ref=design_ref,
        requested_stage_ref=(
            dsp_stage_binding(requested_stage)
            if requested_stage is not None
            else None
        ),
        readback_stage_ref=(
            dsp_stage_binding(readback_stage)
            if readback_stage is not None
            else None
        ),
        predicted_stage_ref=(
            dsp_stage_binding(predicted_stage)
            if predicted_stage is not None
            else None
        ),
        measured_stage_ref=(
            dsp_stage_binding(measured_stage)
            if measured_stage is not None
            else None
        ),
        mapping_ref=(
            dsp_mapping_binding(mapping)
            if mapping is not None
            else None
        ),
        active_sample_rate_hz=active_rate,
        state=state,
        readback_status=readback_status,
        transfer_verification=transfer_verification,
        error_components=tuple(
            (component, errors[component])
            for component in ALL_DSP_ERROR_COMPONENTS
            if component in errors
        ),
        reasons=tuple(reasons),
        evaluation_version=DSPREAL_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadDspRealizationQualification, payload,
        'qualification_id', 'qualification_sha256', 'dspqual',
    )


__all__ = [
    'ALL_DSP_ERROR_COMPONENTS',
    'ALL_DSP_STAGES',
    'CadDspFilterBank',
    'CadDspGridMapResult',
    'CadDspParameterGrid',
    'CadDspParameterMapping',
    'CadDspParameterMappingEntry',
    'CadDspRealizationProfile',
    'CadDspRealizationQualification',
    'CadDspStageRecord',
    'CoefficientExposure',
    'CoefficientFormat',
    'DSPREAL_AUTHORITY_SCHEMA_VERSION',
    'DSPREAL_EVALUATION_VERSION',
    'DspErrorComponent',
    'DspFilterFamily',
    'DspParameterConvention',
    'DspParameterKind',
    'DspRealizationState',
    'DspStageKind',
    'MappingVerdict',
    'ProcessingOrderState',
    'RoundingRule',
    'TransferVerificationState',
    'build_dsp_parameter_mapping',
    'build_dsp_realization_profile',
    'build_dsp_stage_record',
    'dsp_mapping_binding',
    'dsp_profile_binding',
    'dsp_stage_binding',
    'evaluate_dsp_realization',
]
