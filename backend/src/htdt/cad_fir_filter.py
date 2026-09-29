"""FIR10: authority for time-domain room-correction filters (#978).

An ``FIRFilterArtifact`` is an immutable, content-addressed time-domain
filter: coefficient identity (tap vector + tap hash), sample-rate domain,
normalization/time origin, and a *derived* phase class — never a decorative
label. Ordered chains compose declared filters and report total latency as
filter latency, explicitly distinct from speaker-alignment delay.

The module represents, evaluates, imports and materializes FIR filters. It
never synthesizes them (no window design, no per-tap optimization, no
Dirac/Trinnov cloning) and never performs magnitude-only evaluation: every
diagnostic reports magnitude, phase and group delay together.
"""

from __future__ import annotations

from hashlib import sha256
from math import atan2, degrees, isfinite, log10, pi, sqrt
from typing import Any, Literal, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .cad_auralization import _resample_band_limited
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest, canonicalize_payload


FIR_SCHEMA_VERSION = 1
FIR_ARTIFACT_AUTHORITY_VERSION = 'fir10-fir-filter-1'
FIR_IMPORT_AUTHORITY_VERSION = 'fir10-fir-import-1'
FIR_MATERIALIZATION_AUTHORITY_VERSION = 'fir10-fir-materialization-2'
FIR_CAPABILITY_AUTHORITY_VERSION = 'fir10-device-fir-capability-1'
FIR_EVALUATION_VERSION = 'fir-response-v1'

#: Longest impulse the exact pole/zero phase classification supports. Roots
#: of a 512-degree polynomial are already a heavy computation; beyond this
#: the class falls back to symmetry analysis only and fails closed to
#: 'arbitrary_fir' rather than trusting a label.
_FIR_CLASSIFICATION_MAX_TAPS = 512
_SYMMETRY_TOLERANCE = 1e-9
_UNIT_CIRCLE_TOLERANCE = 1e-6






def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


FIRFilterClass = Literal[
    'arbitrary_fir',
    'linear_phase',
    'minimum_phase',
    'mixed_phase',
    'imported_opaque_fir',
]

FIRTapFormat = Literal['float32', 'float64', 'pcm16', 'pcm24', 'unknown']

FIRNormalization = Literal[
    'none',
    'unity_dc_gain',
    'unity_peak',
    'imported',
]

FIRSourceFormat = Literal[
    'raw_float_taps',
    'equalizer_apo_convolution_txt',
    'minidsp_text_export',
]

FIRSupportState = Literal['supported', 'unsupported', 'unknown']
FIRMaterializationVerdict = Literal['supported', 'limited', 'unsupported']


class FIRSourceProvenance(BaseModel):
    """Where the coefficient set came from; an import never becomes 'designed'."""

    model_config = ConfigDict(frozen=True)

    evidence_kind: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_reference: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )


class FIRFilterArtifact(BaseModel):
    """Immutable, content-addressed time-domain correction filter."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = FIR_SCHEMA_VERSION
    authority_version: Literal[
        'fir10-fir-filter-1'
    ] = FIR_ARTIFACT_AUTHORITY_VERSION

    artifact_id: str = Field(pattern=r'^fir-filter:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    #: Derived phase class. Specific claims ('linear_phase',
    #: 'minimum_phase', 'mixed_phase') are recomputed from the tap vector at
    #: build time; 'arbitrary_fir' and 'imported_opaque_fir' are opaque
    #: declarations flagged by ``class_verified``.
    filter_class: FIRFilterClass
    class_verified: bool

    sample_rate_hz: float = Field(gt=0.0)
    taps: tuple[float, ...] = Field(min_length=1)
    taps_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    tap_format: FIRTapFormat
    #: Tap values as quantized to ``tap_format`` already live in ``taps``;
    #: this records the quantization step so materialization stays explicit.
    tap_quantization_step: float | None = Field(default=None, gt=0.0)

    #: Channel the artifact is bound to; never silently applied elsewhere.
    channel_id: str = Field(min_length=1)
    gain_db: float = 0.0
    normalization: FIRNormalization = 'none'
    #: Time origin the pre/post-ringing split and phase reference use.
    time_reference_sample: int = Field(ge=0)
    #: Declared processing latency in seconds — a property of the filter's
    #: processing position, deliberately NOT the speaker-alignment delay.
    latency_s: float = Field(ge=0.0, default=0.0)

    source_producer: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    provenance: tuple[FIRSourceProvenance, ...] = ()
    #: Hash of the raw source bytes when the artifact was imported.
    raw_source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @field_validator('sample_rate_hz', 'gain_db', 'latency_s')
    @classmethod
    def finite_numbers(cls, value: float) -> float:
        return _finite(value, field_name='numeric field')

    @model_validator(mode='after')
    def valid_artifact(self) -> 'FIRFilterArtifact':
        if any(not isfinite(float(tap)) for tap in self.taps):
            raise ValueError('fir taps must be finite')
        if self.time_reference_sample >= len(self.taps):
            raise ValueError('time reference sample outside the tap vector')
        if self.taps_sha256 != _digest(
            {
                'taps': [float(t) for t in self.taps],
                'sample_rate_hz': float(self.sample_rate_hz),
            }
        ):
            raise ValueError('fir taps content hash mismatch')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('fir filter semantic hash mismatch')
        if self.artifact_id != f'fir-filter:{expected}':
            raise ValueError('fir filter id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )

    @property
    def duration_s(self) -> float:
        return len(self.taps) / float(self.sample_rate_hz)


def _linear_phase_score(taps: tuple[float, ...]) -> bool:
    """True when taps are symmetric or antisymmetric within tolerance."""
    n = len(taps)
    if n < 2:
        return True
    # Vectorized pair checks: same elementwise differences/sums and the
    # same tolerance comparison as the scalar loop over i in range(n//2);
    # for odd n the middle tap is still deliberately unconstrained.
    front = np.asarray(taps[: n // 2], dtype=np.float64)
    back = np.asarray(taps[n - 1 : n - 1 - n // 2 : -1], dtype=np.float64)
    symmetric = bool(np.all(np.abs(front - back) <= _SYMMETRY_TOLERANCE))
    antisymmetric = bool(np.all(np.abs(front + back) <= _SYMMETRY_TOLERANCE))
    return symmetric or antisymmetric


def _minimum_phase_confirmed(taps: tuple[float, ...]) -> bool:
    """Every polynomial zero inside the unit circle within tolerance."""
    if len(taps) < 2 or len(taps) > _FIR_CLASSIFICATION_MAX_TAPS:
        return False
    if abs(taps[0]) <= _SYMMETRY_TOLERANCE:
        return False
    roots = np.roots(np.asarray(taps, dtype=float))
    if roots.size == 0:
        return False
    return bool(np.all(np.abs(roots) <= 1.0 + _UNIT_CIRCLE_TOLERANCE))


def derive_fir_class(taps: Sequence[float]) -> FIRFilterClass:
    """Derive the verifiable phase class of a tap vector.

    'imported_opaque_fir' is never derived — it is a declaration about
    evidence, not a property of the coefficients.
    """
    vector = tuple(float(t) for t in taps)
    if _linear_phase_score(vector):
        return 'linear_phase'
    if _minimum_phase_confirmed(vector):
        return 'minimum_phase'
    return 'mixed_phase'


def build_fir_filter_artifact(**kwargs: Any) -> FIRFilterArtifact:
    """Construct an artifact, deriving/verifying its phase class.

    A caller may declare a specific class only when the derivation agrees;
    'arbitrary_fir' and 'imported_opaque_fir' are opaque declarations
    (``class_verified`` False). A mismatched specific claim fails closed.
    """
    taps = tuple(float(t) for t in kwargs['taps'])
    kwargs['taps'] = taps
    declared = kwargs.get('filter_class', 'arbitrary_fir')
    derived = derive_fir_class(taps)
    if declared in ('arbitrary_fir', 'imported_opaque_fir'):
        class_verified = False
    elif declared != derived:
        raise ValueError(
            f'declared fir class {declared!r} disagrees with the derived '
            f'class {derived!r}'
        )
    else:
        class_verified = True
    kwargs['filter_class'] = declared
    kwargs['class_verified'] = class_verified
    kwargs['taps_sha256'] = _digest(
        {
            'taps': [float(t) for t in taps],
            'sample_rate_hz': float(kwargs['sample_rate_hz']),
        }
    )
    candidate = FIRFilterArtifact.model_construct(**canonicalize_payload(FIRFilterArtifact, dict(
        **kwargs,
        artifact_id='fir-filter:' + '0' * 64,
        semantic_sha256='0' * 64,
    )))
    digest = _digest(candidate.semantic_payload())
    return FIRFilterArtifact(
        **kwargs,
        artifact_id=f'fir-filter:{digest}',
        semantic_sha256=digest,
    )


class FIRImportRecord(BaseModel):
    """Evidence that an external coefficient set was imported exactly.

    An imported FIR is evidence — bytes, rate and parser version preserved —
    never silently treated as applied device state.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = FIR_SCHEMA_VERSION
    authority_version: Literal[
        'fir10-fir-import-1'
    ] = FIR_IMPORT_AUTHORITY_VERSION
    import_id: str = Field(pattern=r'^fir-import:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    artifact_id: str = Field(pattern=r'^fir-filter:[0-9a-f]{64}$')
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_format: FIRSourceFormat
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_byte_count: int = Field(gt=0)
    declared_sample_rate_hz: float = Field(gt=0.0)
    declared_channel_id: str | None = Field(default=None, min_length=1)
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_import(self) -> 'FIRImportRecord':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('fir import semantic hash mismatch')
        if self.import_id != f'fir-import:{expected}':
            raise ValueError('fir import id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'import_id', 'semantic_sha256'},
        )


def _parse_tap_text(source_bytes: bytes) -> tuple[float, ...]:
    """Whitespace/comma-separated decimal taps; one tap per token."""
    text = source_bytes.decode('utf-8')
    values: list[float] = []
    for token in text.replace(',', ' ').split():
        value = float(token)
        if not isfinite(value):
            raise ValueError('imported fir tap is not finite')
        values.append(float(value))
    if not values:
        raise ValueError('imported fir source contains no taps')
    return tuple(values)


def import_fir_filter_artifact(
    *,
    source_bytes: bytes,
    source_format: FIRSourceFormat,
    declared_sample_rate_hz: float,
    channel_id: str,
    parser_id: str = 'htdt.fir-text',
    parser_version: str = '1',
    tap_format: FIRTapFormat = 'float64',
    time_reference_sample: int = 0,
    latency_s: float = 0.0,
    declared_channel_id: str | None = None,
) -> tuple[FIRFilterArtifact, FIRImportRecord]:
    """Import an external FIR as evidence; never as applied device state.

    Only documented formats parse. Anything else fails closed rather than
    guessing at a byte layout.
    """
    if source_format == 'minidsp_text_export':
        raise ValueError(
            'minidsp_text_export parsing is not implemented in FIR10; the '
            'format is declared but its layout is not yet documented'
        )
    if source_format not in (
        'raw_float_taps',
        'equalizer_apo_convolution_txt',
    ):
        raise ValueError(f'unsupported fir import format {source_format!r}')
    taps = _parse_tap_text(source_bytes)
    source_sha256 = sha256(source_bytes).hexdigest()
    artifact = build_fir_filter_artifact(
        filter_class='imported_opaque_fir',
        sample_rate_hz=declared_sample_rate_hz,
        taps=taps,
        tap_format=tap_format,
        channel_id=channel_id,
        normalization='imported',
        time_reference_sample=time_reference_sample,
        latency_s=latency_s,
        source_producer=parser_id,
        source_version=parser_version,
        provenance=(
            FIRSourceProvenance(
                evidence_kind='imported',
                source_name=source_format,
                source_version=parser_version,
                source_sha256=source_sha256,
            ),
        ),
        raw_source_sha256=source_sha256,
    )
    candidate = FIRImportRecord.model_construct(**canonicalize_payload(FIRImportRecord, dict(
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.semantic_sha256,
        source_format=source_format,
        source_sha256=source_sha256,
        source_byte_count=len(source_bytes),
        declared_sample_rate_hz=float(declared_sample_rate_hz),
        declared_channel_id=declared_channel_id,
        parser_id=parser_id,
        parser_version=parser_version,
        import_id='fir-import:' + '0' * 64,
        semantic_sha256='0' * 64,
    )))
    digest = _digest(candidate.semantic_payload())
    record = FIRImportRecord(
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.semantic_sha256,
        source_format=source_format,
        source_sha256=source_sha256,
        source_byte_count=len(source_bytes),
        declared_sample_rate_hz=float(declared_sample_rate_hz),
        declared_channel_id=declared_channel_id,
        parser_id=parser_id,
        parser_version=parser_version,
        import_id=f'fir-import:{digest}',
        semantic_sha256=digest,
    )
    return artifact, record


class DeviceFIRCapabilityProfile(BaseModel):
    """What a device can do with time-domain filters; None means unknown."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = FIR_SCHEMA_VERSION
    authority_version: Literal[
        'fir10-device-fir-capability-1'
    ] = FIR_CAPABILITY_AUTHORITY_VERSION
    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    support_state: FIRSupportState
    supported_sample_rates_hz: tuple[float, ...] | None = None
    max_taps_per_channel: int | None = Field(default=None, ge=0)
    max_filter_duration_s: float | None = Field(default=None, gt=0.0)
    per_channel_independent: bool | None = None
    tap_format: FIRTapFormat | None = None
    coefficient_quantization_step: float | None = Field(default=None, gt=0.0)
    normalization: FIRNormalization | None = None
    max_processing_latency_s: float | None = Field(default=None, ge=0.0)
    import_formats: tuple[FIRSourceFormat, ...] = ()
    processing_position: Literal[
        'pre_output', 'post_eq', 'unknown'
    ] = 'unknown'

    @field_validator(
        'supported_sample_rates_hz',
        'import_formats',
        mode='before',
    )
    @classmethod
    def unique_tuples(cls, value: Any) -> Any:
        return value


class FIRResponseDiagnostics(BaseModel):
    """Derived frequency-domain response; never magnitude-only."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    evaluation_version: str
    frequencies_hz: tuple[float, ...]
    magnitude_db: tuple[float, ...]
    phase_deg: tuple[float, ...]
    group_delay_s: tuple[float, ...]
    impulse_peak: float
    norm_l2: float
    #: Ringing diagnostics relative to ``time_reference_sample``.
    pre_ringing_energy: float
    post_ringing_energy: float
    pre_event_peak_amplitude: float
    total_latency_s: float


class FIRChainDiagnostics(BaseModel):
    """Ordered-chain response: composition follows declaration order."""

    model_config = ConfigDict(frozen=True)

    artifact_ids: tuple[str, ...]
    order_state: Literal['declared', 'unknown']
    frequencies_hz: tuple[float, ...]
    total_magnitude_db: tuple[float, ...]
    total_phase_deg: tuple[float, ...]
    total_group_delay_s: tuple[float, ...]
    total_latency_s: float


def _artifact_response(
    artifact: FIRFilterArtifact,
    frequencies_hz: Sequence[float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Complex response, unwrapped phase and group delay at given freqs."""
    taps = np.asarray(artifact.taps, dtype=float)
    freqs = np.asarray(frequencies_hz, dtype=float)
    if freqs.size == 0:
        raise ValueError('frequency grid is empty')
    if np.any(freqs <= 0.0) or np.any(freqs > artifact.sample_rate_hz / 2.0):
        raise ValueError(
            'fir evaluation frequencies outside (0, Nyquist]'
        )
    n = taps.size
    k = np.arange(n)
    phase_terms = np.exp(-2j * pi * np.outer(freqs, k) / artifact.sample_rate_hz)
    response = phase_terms @ taps
    # Constant output gain folds into the complex response.
    try:
        output_gain = 10.0 ** (artifact.gain_db / 20.0)
    except OverflowError as exc:
        raise ValueError('fir artifact gain_db out of representable range') from exc
    response = response * output_gain
    magnitude = np.abs(response)
    phase = np.unwrap(np.angle(response))
    # Analytic group delay -Im(H'/H) per evaluation frequency: a finite
    # difference of the unwrapped phase over the caller's grid aliases
    # whenever adjacent points span more than pi of phase.
    derivative = ((-1j * k / artifact.sample_rate_hz) * phase_terms) @ taps
    with np.errstate(divide='ignore', invalid='ignore'):
        group_delay = -np.imag(derivative / response)
    group_delay = np.nan_to_num(group_delay, nan=0.0)
    return response, phase, group_delay


def evaluate_fir_artifact(
    artifact: FIRFilterArtifact,
    frequencies_hz: Sequence[float],
) -> FIRResponseDiagnostics:
    """Magnitude/phase/group-delay plus ringing diagnostics for one artifact."""
    frequencies = tuple(float(f) for f in frequencies_hz)
    response, phase, group_delay = _artifact_response(artifact, frequencies)
    taps = np.asarray(artifact.taps, dtype=float)
    reference = artifact.time_reference_sample
    magnitude_db = tuple(
        20.0 * log10(max(float(np.abs(bin_value)), 1e-30))
        for bin_value in response
    )
    return FIRResponseDiagnostics(
        artifact_id=artifact.artifact_id,
        evaluation_version=FIR_EVALUATION_VERSION,
        frequencies_hz=frequencies,
        magnitude_db=magnitude_db,
        phase_deg=tuple(float(degrees(p)) for p in phase),
        group_delay_s=tuple(float(v) for v in group_delay),
        impulse_peak=float(np.max(np.abs(taps))),
        norm_l2=float(sqrt(float(np.sum(taps * taps)))),
        pre_ringing_energy=float(np.sum(taps[:reference] ** 2)),
        post_ringing_energy=float(np.sum(taps[reference + 1 :] ** 2)),
        pre_event_peak_amplitude=float(
            np.max(np.abs(taps[:reference])) if reference > 0 else 0.0
        ),
        total_latency_s=float(artifact.latency_s),
    )


def evaluate_fir_chain(
    artifacts: Sequence[FIRFilterArtifact],
    frequencies_hz: Sequence[float],
    *,
    order_state: Literal['declared', 'unknown'] = 'declared',
) -> FIRChainDiagnostics:
    """Compose an ordered chain; order 'unknown' never pretends to be known."""
    if not artifacts:
        raise ValueError('fir chain is empty')
    rates = {a.sample_rate_hz for a in artifacts}
    if len(rates) != 1:
        raise ValueError(
            'fir chain evaluation requires one common sample rate; '
            'materialize members to a single rate first'
        )
    frequencies = tuple(float(f) for f in frequencies_hz)
    total_response = np.ones(len(frequencies), dtype=complex)
    total_phase = np.zeros(len(frequencies))
    total_delay = np.zeros(len(frequencies))
    total_latency = 0.0
    for artifact in artifacts:
        response, phase, group_delay = _artifact_response(
            artifact, frequencies
        )
        total_response = total_response * response
        total_phase = total_phase + phase
        total_delay = total_delay + group_delay
        total_latency += artifact.latency_s
    return FIRChainDiagnostics(
        artifact_ids=tuple(a.artifact_id for a in artifacts),
        order_state=order_state,
        frequencies_hz=frequencies,
        total_magnitude_db=tuple(
            20.0 * log10(max(float(np.abs(b)), 1e-30)) for b in total_response
        ),
        total_phase_deg=tuple(float(degrees(p)) for p in total_phase),
        total_group_delay_s=tuple(float(v) for v in total_delay),
        total_latency_s=total_latency,
    )


def evaluate_device_fir_support(
    artifact: FIRFilterArtifact,
    capability: DeviceFIRCapabilityProfile,
) -> tuple[FIRMaterializationVerdict, tuple[str, ...]]:
    """Fail-closed device support check.

    'SUPPORTED' means the artifact fits as declared; 'LIMITED' means it can
    be made to fit only through an explicit materialization (resample,
    truncation or quantization — the caller must materialize, this function
    never does it implicitly); 'UNSUPPORTED' means no materialization is
    possible under the declared capability. Unknown is not unlimited.
    """
    reasons: list[str] = []
    limited = False
    if capability.support_state == 'unknown':
        return ('unsupported', ('device fir capability is unknown',))
    if capability.support_state == 'unsupported':
        return ('unsupported', ('device does not support fir processing',))
    if capability.supported_sample_rates_hz is not None and (
        float(artifact.sample_rate_hz)
        not in {float(r) for r in capability.supported_sample_rates_hz}
    ):
        reasons.append(
            'artifact sample rate not in device supported rates; '
            'explicit resample materialization required'
        )
        limited = True
    if capability.max_taps_per_channel is not None and (
        len(artifact.taps) > capability.max_taps_per_channel
    ):
        reasons.append(
            'artifact tap count exceeds device per-channel limit; '
            'explicit truncation materialization required'
        )
        limited = True
    if capability.max_filter_duration_s is not None and (
        artifact.duration_s > capability.max_filter_duration_s
    ):
        reasons.append(
            'artifact duration exceeds device filter duration limit'
        )
        limited = True
    if capability.tap_format is not None and (
        artifact.tap_format != capability.tap_format
    ):
        reasons.append('device requires a different coefficient format')
        limited = True
    if capability.max_processing_latency_s is not None and (
        artifact.latency_s > capability.max_processing_latency_s
    ):
        return (
            'unsupported',
            tuple(reasons) + ('artifact latency exceeds device maximum',),
        )
    verdict: FIRMaterializationVerdict = (
        'limited' if limited else 'supported'
    )
    return (verdict, tuple(reasons))


class FIRMaterializationReport(BaseModel):
    """Explicit resample/truncate/quantize record with error diagnostics."""

    model_config = ConfigDict(frozen=True)

    source_artifact_id: str
    source_artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    materialized_artifact_id: str
    materialized_artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    resampled: bool
    truncated_tap_count: int = Field(ge=0)
    quantized: bool
    #: Worst response deviation the materialization introduced in-band.
    max_response_error_db: float
    evaluation_version: str = FIR_MATERIALIZATION_AUTHORITY_VERSION


def _resample_taps(
    taps: np.ndarray,
    source_rate: float,
    target_rate: float,
) -> np.ndarray:
    """Band-limited resample of the impulse response itself.

    Reuses ``htdt.fft_bandlimited_resample_v1`` so downsampled taps are
    brick-wall anti-aliased and upsampled taps are sinc-reconstructed
    rather than linearly interpolated (linear interpolation leaves a
    sinc^2 droop reaching ~-8 dB at the new Nyquist and folds out-of-band
    content into the passband on decimation).
    """
    if abs(target_rate - source_rate) < 1e-9:
        return np.asarray(taps, dtype=float)
    return _resample_band_limited(
        np.asarray(taps, dtype=float), source_rate, target_rate
    )


def materialize_fir_artifact(
    artifact: FIRFilterArtifact,
    *,
    target_sample_rate_hz: float | None = None,
    max_tap_count: int | None = None,
    tap_format: FIRTapFormat | None = None,
    quantization_step: float | None = None,
) -> tuple[FIRFilterArtifact, FIRMaterializationReport]:
    """Produce a NEW artifact for a device contract; never mutate in place.

    Every deviation is explicit: resample, truncation and quantization are
    applied deterministically and the worst in-band response error is
    reported — requested vs exported stays distinguishable.
    """
    rate = float(
        target_sample_rate_hz
        if target_sample_rate_hz is not None
        else artifact.sample_rate_hz
    )
    if rate <= 0.0:
        raise ValueError('target sample rate must be positive')
    taps = _resample_taps(
        np.asarray(artifact.taps, dtype=float),
        float(artifact.sample_rate_hz),
        rate,
    )
    truncated = 0
    if max_tap_count is not None:
        if max_tap_count < 1:
            raise ValueError('max_tap_count must be positive')
        if len(taps) > max_tap_count:
            truncated = len(taps) - max_tap_count
            # Preserve the time origin: drop the tail, shift nothing.
            taps = taps[:max_tap_count]
    # The time reference marks a physical instant (t=0 for the ringing
    # split); its index must scale with the resample ratio, not clamp the
    # source index into the new grid.
    reference = min(
        int(
            round(
                artifact.time_reference_sample
                * rate
                / float(artifact.sample_rate_hz)
            )
        ),
        len(taps) - 1,
    )
    new_format = tap_format if tap_format is not None else artifact.tap_format
    quantized = False
    step = (
        quantization_step
        if quantization_step is not None
        else (
            1.0 / 32768.0
            if new_format == 'pcm16'
            else (
                1.0 / 8388608.0
                if new_format == 'pcm24'
                else None
            )
        )
    )
    if step is not None:
        taps = np.round(taps / step) * step
        if new_format in ('pcm16', 'pcm24'):
            # Signed PCM saturates one format LSB below +full scale: +1.0
            # is not representable (int16 tops at 32767/32768), so taps
            # clamp to the format's representable range.
            format_lsb = (
                1.0 / 32768.0 if new_format == 'pcm16' else 1.0 / 8388608.0
            )
            taps = np.clip(taps, -1.0, 1.0 - format_lsb)
        quantized = True
    if new_format == 'float32':
        taps = np.asarray(taps, dtype=np.float32).astype(float)
    # The materialized tap vector gets its own derived class: truncation or
    # resampling honestly changes the phase property, so the new artifact
    # never inherits the source's class declaration.
    materialized = build_fir_filter_artifact(
        filter_class=derive_fir_class(tuple(float(t) for t in taps)),
        sample_rate_hz=rate,
        taps=tuple(float(t) for t in taps),
        tap_format=new_format,
        tap_quantization_step=step,
        channel_id=artifact.channel_id,
        gain_db=artifact.gain_db,
        normalization=artifact.normalization,
        time_reference_sample=reference,
        latency_s=artifact.latency_s,
        source_producer='htdt.fir-materialization',
        source_version=FIR_MATERIALIZATION_AUTHORITY_VERSION,
        provenance=artifact.provenance
        + (
            FIRSourceProvenance(
                evidence_kind='materialization',
                source_name='htdt.fir-materialization',
                source_version=FIR_MATERIALIZATION_AUTHORITY_VERSION,
                source_reference=artifact.artifact_id,
                source_sha256=artifact.semantic_sha256,
            ),
        ),
        raw_source_sha256=artifact.raw_source_sha256,
    )
    # Worst in-band response deviation versus the source artifact.
    nyquist = min(artifact.sample_rate_hz, rate) / 2.0
    grid = tuple(
        nyquist * (i + 1) / 32.0 for i in range(32)
    )
    source_diag = evaluate_fir_artifact(artifact, grid)
    new_diag = evaluate_fir_artifact(materialized, grid)
    max_error = max(
        abs(a - b)
        for a, b in zip(
            source_diag.magnitude_db, new_diag.magnitude_db
        )
    )
    report = FIRMaterializationReport(
        source_artifact_id=artifact.artifact_id,
        source_artifact_sha256=artifact.semantic_sha256,
        materialized_artifact_id=materialized.artifact_id,
        materialized_artifact_sha256=materialized.semantic_sha256,
        resampled=abs(rate - artifact.sample_rate_hz) > 1e-9,
        truncated_tap_count=truncated,
        quantized=quantized,
        max_response_error_db=float(max_error),
    )
    return materialized, report
