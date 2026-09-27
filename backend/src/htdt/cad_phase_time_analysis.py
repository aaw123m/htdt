"""Phase/time derived-analysis authority (#971).

``CadFrequencyResponseDataset`` can retain raw phase (#489) and #642 owns
the common timing reference — but group delay, minimum/excess phase and
crossover-region temporal alignment are *derived* observables whose
meaning depends on exact spec semantics: IR window, smoothing, t=0 /
delay-removal state, LF/HF extension, warping and the algorithm identity.

This module adds:

- ``PhaseTimeAnalysisSpec`` — the immutable spec a saved result replays
  from;
- ``PhaseTimeAnalysisResult`` — sealed derived traces (group delay,
  minimum phase, excess phase) bound to the exact spec and dataset;
- deterministic native reference algorithms
  (``htdt_group_delay_v1`` / ``htdt_min_phase_v1``) implementing unwrap +
  smoothed phase-slope differentiation and Hilbert-transform minimum
  phase, always labelled by provider/algorithm identity — a REW-derived
  result keeps REW as provider and is never presented as an HTDT
  recomputation;
- ``CrossoverAlignmentDiagnostic`` — pairwise temporal alignment that
  fails closed unless both sides share a compatible timing reference and
  routing/normalization semantics.

Raw measured traces are never mutated: transformed variants are new
derived results. Manual t=0/delay removal cannot retain absolute-timing
authority — ``absolute_timing_state`` drops to ``removed``.
"""

from __future__ import annotations

from math import atan2, cos, isfinite, pi, sin
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash






_SHA256_PATTERN = r'^[0-9a-f]{64}$'
TWO_PI = 2.0 * pi

HTDT_GROUP_DELAY_ALGORITHM = 'htdt_group_delay_v1'
HTDT_MIN_PHASE_ALGORITHM = 'htdt_min_phase_v1'


AnalysisProvider = Literal['rew_producer', 'htdt', 'imported', 'unknown']
"""Who computed the derived trace. ``rew_producer`` results keep REW's
own algorithm identity/version; they are never relabeled as a native
HTDT recomputation."""

DelayRemovalState = Literal['not_applied', 'applied', 'producer_applied', 'unknown']

AbsoluteTimingState = Literal['retained', 'removed', 'producer', 'unknown']
"""Whether the analysis preserves absolute-timing authority. Any manual
t=0/delay-removal transform must report ``removed`` — the absolute delay
claim cannot silently survive a manual shift."""

TailExtensionPolicy = Literal[
    'none', 'flat', 'linear_slope', 'producer', 'unknown'
]

AnalysisBandState = Literal['full', 'limited', 'insufficient', 'unknown']


def unwrap_phase_deg(phase_deg: tuple[float, ...]) -> tuple[float, ...]:
    """Deterministic phase unwrap (2π discontinuity correction)."""
    if not phase_deg:
        return ()
    out = [float(phase_deg[0])]
    offset = 0.0
    previous = float(phase_deg[0])
    for raw in phase_deg[1:]:
        value = float(raw)
        delta = value - previous
        if delta > 180.0:
            offset -= 360.0
        elif delta < -180.0:
            offset += 360.0
        out.append(value + offset)
        previous = value
    return tuple(out)


def compute_group_delay_s(
    frequency_hz: tuple[float, ...],
    phase_deg: tuple[float, ...],
    *,
    smoothing_samples: int = 0,
) -> tuple[float, ...]:
    """Group delay ``-dφ/dω`` from unwrapped phase (``htdt_group_delay_v1``).

    Phase is unwrapped first; differentiation is a central difference on
    the (optionally box-smoothed) unwrapped trace — wrapped or noisy phase
    is never differentiated raw. Returns seconds per frequency sample.
    """
    count = len(frequency_hz)
    if len(phase_deg) != count:
        raise ValueError('phase length must match frequency axis')
    if count < 3:
        raise ValueError('group delay requires at least three samples')
    if any(not isfinite(f) or f <= 0 for f in frequency_hz):
        raise ValueError('frequency axis must be finite and positive')
    if any(
        frequency_hz[i] <= frequency_hz[i - 1] for i in range(1, count)
    ):
        raise ValueError('frequency axis must be strictly increasing')
    if any(not isfinite(p) for p in phase_deg):
        raise ValueError('phase values must be finite')
    if smoothing_samples < 0:
        raise ValueError('smoothing_samples must be >= 0')

    unwrapped = unwrap_phase_deg(phase_deg)
    if smoothing_samples:
        half = smoothing_samples
        smoothed = []
        for index in range(count):
            low = max(0, index - half)
            high = min(count, index + half + 1)
            smoothed.append(sum(unwrapped[low:high]) / (high - low))
        unwrapped = tuple(smoothed)

    delay_s: list[float] = []
    for index in range(count):
        low = index - 1 if index > 0 else 0
        high = index + 1 if index < count - 1 else count - 1
        d_phase_deg = unwrapped[high] - unwrapped[low]
        d_omega = TWO_PI * (frequency_hz[high] - frequency_hz[low])
        delay_s.append(-(d_phase_deg * pi / 180.0) / d_omega)
    return tuple(delay_s)


def _dft(values: tuple[float, ...], inverse: bool = False) -> list[complex]:
    """O(n²) reference DFT for the native min-phase derivation."""
    n = len(values)
    sign = 1.0 if inverse else -1.0
    scale = 1.0 / n if inverse else 1.0
    out: list[complex] = []
    for k in range(n):
        acc = 0j
        for m in range(n):
            angle = sign * TWO_PI * k * m / n
            acc += values[m] * complex(cos(angle), sin(angle))
        out.append(acc * scale)
    return out


def compute_minimum_phase_deg(
    frequency_hz: tuple[float, ...],
    level_db: tuple[float, ...],
) -> tuple[float, ...]:
    """Minimum-phase response for a magnitude response (``htdt_min_phase_v1``).

    Reference implementation: real cepstrum of the linear magnitude,
    minimum-phase liftering (ceps[n>0] * 2), DFT back, then the phase of
    the resulting transfer function — unwrapped for comparability. The
    honest limitations (frequency grid not uniformly spaced is rejected,
    finite-span tails) are enforced by the spec validators, not hidden.
    """
    count = len(level_db)
    if len(frequency_hz) != count:
        raise ValueError('level length must match frequency axis')
    if count < 4:
        raise ValueError('minimum phase requires at least four samples')
    if any(not isfinite(v) for v in level_db):
        raise ValueError('level values must be finite')
    # The liftering reference implementation assumes a uniform grid.
    if count > 1:
        step = frequency_hz[1] - frequency_hz[0]
        if step <= 0.0:
            raise ValueError('frequency axis must be increasing')
        for index in range(2, count):
            if abs((frequency_hz[index] - frequency_hz[index - 1]) - step) > max(
                1e-9, abs(step) * 1e-6
            ):
                raise ValueError(
                    'htdt_min_phase_v1 requires a uniform frequency grid; '
                    'resample under an explicit spec or use a producer result'
                )
        # The cepstral lifter assumes the samples span the unit circle —
        # a sub-band axis that does not reach DC (within half a bin) cannot
        # reconstruct the true minimum phase and would silently fabricate
        # precision.
        if frequency_hz[0] > step * 0.5:
            raise ValueError(
                'htdt_min_phase_v1 requires a DC-to-Nyquist uniform grid; '
                'sub-band measurement axes need a producer-derived minimum '
                'phase instead'
            )
    magnitudes = tuple(10.0 ** (v / 20.0) for v in level_db)
    floor = min(magnitudes)
    if floor <= 0.0:
        magnitudes = tuple(max(v, floor * 1e-3, 1e-12) for v in magnitudes)
    # Homomorphic minimum phase: mirror the one-sided log magnitude into a
    # two-sided even-symmetric spectrum so the cepstral lifter applies the
    # standard discrete Hilbert relationship.
    log_mag = tuple(_safe_log(v) for v in magnitudes)
    two_sided = log_mag + tuple(log_mag[i] for i in range(count - 2, 0, -1))
    size = len(two_sided)
    cepstrum = _dft(two_sided, inverse=True)
    liftered = [cepstrum[0]]
    liftered += [2.0 * cepstrum[i] for i in range(1, size // 2)]
    liftered.append(cepstrum[size // 2])
    liftered += [0.0] * (size - len(liftered))
    spectrum = _dft(tuple(liftered))
    min_phase = [atan2(spectrum[i].imag, spectrum[i].real) for i in range(count)]
    return unwrap_phase_deg(tuple(p * 180.0 / pi for p in min_phase))


def _safe_log(value: float) -> float:
    from math import log

    return log(max(value, 1e-30))


class PhaseTimeAnalysisSpec(BaseModel):
    """Immutable spec a derived phase/time result replays from."""

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    ir_dataset_id: str | None = None
    ir_dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    timing_reference_id: str | None = None
    timing_reference_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    phase_capability: Literal['valid', 'absent', 'unknown'] = 'unknown'
    ir_window_id: str | None = None
    ir_window_start_s: float | None = None
    ir_window_end_s: float | None = None
    smoothing: str | None = None
    frequency_grid_ref: str | None = None
    delay_removal_state: DelayRemovalState = 'unknown'
    absolute_timing_state: AbsoluteTimingState = 'unknown'
    min_phase_provider: AnalysisProvider = 'unknown'
    min_phase_algorithm_version: str | None = None
    include_calibration_effects: bool | None = None
    replicate_outside_measured_range: bool | None = None
    lf_extension: TailExtensionPolicy = 'unknown'
    hf_extension: TailExtensionPolicy = 'unknown'
    frequency_warping: str | None = None
    analysis_band_hz: tuple[float, float] | None = None
    band_state: AnalysisBandState = 'unknown'
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    adapter: str | None = None
    adapter_version: str | None = None
    created_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'PhaseTimeAnalysisSpec':
        if self.phase_capability != 'valid':
            raise ValueError(
                'phase/time analysis requires a dataset with valid phase; '
                'magnitude-only evidence needs explicit minimum-phase '
                'derivation semantics first'
            )
        if (
            self.ir_window_start_s is not None
            and self.ir_window_end_s is not None
            and self.ir_window_end_s <= self.ir_window_start_s
        ):
            raise ValueError('IR window end must be after start')
        if (
            self.delay_removal_state == 'applied'
            and self.absolute_timing_state == 'retained'
        ):
            raise ValueError(
                'manual t=0/delay removal cannot retain absolute-timing '
                'authority — set absolute_timing_state to removed'
            )
        if self.analysis_band_hz is not None:
            low, high = self.analysis_band_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('analysis_band_hz must satisfy 0 < low < high')
        if self.band_state == 'insufficient' and self.min_phase_provider != 'unknown':
            raise ValueError(
                'insufficient bandwidth/window evidence cannot produce a '
                'minimum-phase claim — mark provider unknown or improve input'
            )
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('phase/time spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'spec_sha256'})


class DerivedTrace(BaseModel):
    """One derived frequency-domain trace with its own provenance."""

    model_config = ConfigDict(frozen=True)

    trace_kind: Literal['group_delay_s', 'minimum_phase_deg', 'excess_phase_deg', 'excess_group_delay_s']
    frequency_hz: tuple[float, ...]
    values: tuple[float, ...]
    valid_band_hz: tuple[float, float] | None = None
    masked_band_hz: tuple[tuple[float, float], ...] = ()
    algorithm_version: str = Field(min_length=1)
    provider: AnalysisProvider = 'unknown'

    @model_validator(mode='after')
    def valid_trace(self) -> 'DerivedTrace':
        if len(self.frequency_hz) != len(self.values):
            raise ValueError('trace arrays must have equal length')
        if not self.frequency_hz:
            raise ValueError('trace requires a frequency axis')
        for value in (*self.frequency_hz, *self.values):
            if not isfinite(float(value)):
                raise ValueError('trace values must be finite')
        if self.valid_band_hz is not None:
            low, high = self.valid_band_hz
            if not (0 < low < high):
                raise ValueError('valid_band_hz must satisfy 0 < low < high')
        if self.provider == 'unknown':
            raise ValueError('derived trace requires an explicit provider identity')
        return self


class PhaseTimeAnalysisResult(BaseModel):
    """Sealed derived phase/time result bound to one exact spec+dataset."""

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    traces: tuple[DerivedTrace, ...] = ()
    absolute_timing_state: AbsoluteTimingState = 'unknown'
    band_state: AnalysisBandState = 'unknown'
    created_at_utc: str = Field(min_length=1)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_result(self) -> 'PhaseTimeAnalysisResult':
        kinds = [trace.trace_kind for trace in self.traces]
        if len(kinds) != len(set(kinds)):
            raise ValueError('duplicate trace kinds are not allowed')
        if (
            self.absolute_timing_state == 'retained'
            and self.band_state == 'insufficient'
        ):
            raise ValueError(
                'insufficient-band analysis cannot retain absolute timing'
            )
        if self.result_sha256 != _hash(self.identity_payload()):
            raise ValueError('phase/time result hash mismatch')
        return self

    def trace(self, kind: str) -> DerivedTrace | None:
        for trace in self.traces:
            if trace.trace_kind == kind:
                return trace
        return None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'result_sha256'})


class CrossoverAlignmentDiagnostic(BaseModel):
    """Pairwise temporal alignment between two measurements/channels.

    Coherent crossover conclusions require a shared (or explicitly
    reconciled) timing reference plus compatible normalization and
    routing; otherwise the diagnostic fails closed to ``incompatible``.
    """

    model_config = ConfigDict(frozen=True)

    diagnostic_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    pair_a_measurement_id: str = Field(min_length=1)
    pair_b_measurement_id: str = Field(min_length=1)
    pair_a_result_id: str | None = None
    pair_b_result_id: str | None = None
    timing_compatibility: Literal[
        'shared_reference', 'reconciled', 'unshared', 'unknown'
    ] = 'unknown'
    normalization_compatible: bool | None = None
    routing_compatible: bool | None = None
    state: Literal['computable', 'incompatible', 'unknown'] = 'unknown'
    delay_difference_s: float | None = None
    crossover_band_hz: tuple[float, float] | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def valid_diagnostic(self) -> 'CrossoverAlignmentDiagnostic':
        if self.pair_a_measurement_id == self.pair_b_measurement_id:
            raise ValueError('crossover diagnostic requires two distinct measurements')
        if self.state == 'computable':
            if self.timing_compatibility not in ('shared_reference', 'reconciled'):
                raise ValueError(
                    'coherent crossover conclusions require a shared or '
                    'reconciled timing reference'
                )
            if self.normalization_compatible is False:
                raise ValueError('incompatible normalization cannot be computable')
            if self.routing_compatible is False:
                raise ValueError('incompatible routing cannot be computable')
            if self.delay_difference_s is None:
                raise ValueError('computable diagnostic requires delay_difference_s')
        if self.delay_difference_s is not None and not isfinite(float(self.delay_difference_s)):
            raise ValueError('delay_difference_s must be finite')
        return self


def build_phase_time_spec(**kwargs: Any) -> PhaseTimeAnalysisSpec:
    """Assemble and seal a :class:`PhaseTimeAnalysisSpec`."""
    payload = {'spec_sha256': '0' * 64, **kwargs}
    provisional = PhaseTimeAnalysisSpec.model_construct(**payload)
    payload['spec_sha256'] = _hash(provisional.identity_payload())
    return PhaseTimeAnalysisSpec(**payload)


def build_phase_time_result(**kwargs: Any) -> PhaseTimeAnalysisResult:
    """Assemble and seal a :class:`PhaseTimeAnalysisResult`."""
    payload = {'result_sha256': '0' * 64, **kwargs}
    provisional = PhaseTimeAnalysisResult.model_construct(**payload)
    payload['result_sha256'] = _hash(provisional.identity_payload())
    return PhaseTimeAnalysisResult(**payload)


def analyze_group_delay(
    spec: PhaseTimeAnalysisSpec,
    *,
    result_id: str,
    dataset_id: str,
    dataset_sha256: str,
    frequency_hz: tuple[float, ...],
    phase_deg: tuple[float, ...],
    smoothing_samples: int = 0,
    created_at_utc: str,
) -> PhaseTimeAnalysisResult:
    """Run ``htdt_group_delay_v1`` and seal the derived trace."""
    delay_s = compute_group_delay_s(
        frequency_hz, phase_deg, smoothing_samples=smoothing_samples
    )
    trace = DerivedTrace(
        trace_kind='group_delay_s',
        frequency_hz=frequency_hz,
        values=delay_s,
        valid_band_hz=spec.analysis_band_hz,
        algorithm_version=HTDT_GROUP_DELAY_ALGORITHM,
        provider='htdt',
    )
    return build_phase_time_result(
        result_id=result_id,
        spec_id=spec.spec_id,
        spec_sha256=spec.spec_sha256,
        document_id=spec.document_id,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        traces=(trace,),
        absolute_timing_state=spec.absolute_timing_state,
        band_state=spec.band_state,
        created_at_utc=created_at_utc,
    )


def analyze_minimum_excess_phase(
    spec: PhaseTimeAnalysisSpec,
    *,
    result_id: str,
    dataset_id: str,
    dataset_sha256: str,
    frequency_hz: tuple[float, ...],
    level_db: tuple[float, ...],
    measured_phase_deg: tuple[float, ...],
    created_at_utc: str,
) -> PhaseTimeAnalysisResult:
    """Run ``htdt_min_phase_v1`` and seal minimum + excess phase traces."""
    if spec.min_phase_provider not in ('htdt', 'unknown') or (
        spec.min_phase_algorithm_version not in (None, HTDT_MIN_PHASE_ALGORITHM)
    ):
        raise ValueError(
            'native min-phase analysis requires htdt provider semantics; '
            'producer-derived results keep the producer algorithm identity'
        )
    min_phase = compute_minimum_phase_deg(frequency_hz, level_db)
    unwrapped_measured = unwrap_phase_deg(measured_phase_deg)
    if len(unwrapped_measured) != len(min_phase):
        raise ValueError('measured phase axis mismatch')
    excess = tuple(m - mp for m, mp in zip(unwrapped_measured, min_phase))
    traces = (
        DerivedTrace(
            trace_kind='minimum_phase_deg',
            frequency_hz=frequency_hz,
            values=min_phase,
            valid_band_hz=spec.analysis_band_hz,
            algorithm_version=HTDT_MIN_PHASE_ALGORITHM,
            provider='htdt',
        ),
        DerivedTrace(
            trace_kind='excess_phase_deg',
            frequency_hz=frequency_hz,
            values=excess,
            valid_band_hz=spec.analysis_band_hz,
            algorithm_version=HTDT_MIN_PHASE_ALGORITHM,
            provider='htdt',
        ),
    )
    return build_phase_time_result(
        result_id=result_id,
        spec_id=spec.spec_id,
        spec_sha256=spec.spec_sha256,
        document_id=spec.document_id,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        traces=traces,
        absolute_timing_state=spec.absolute_timing_state,
        band_state=spec.band_state,
        created_at_utc=created_at_utc,
    )
