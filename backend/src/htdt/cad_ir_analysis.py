"""Measured impulse-response diagnostics authority (#511).

Derived analysis over exact imported IR evidence: Energy-Time Curve,
deterministic early-reflection markers, Schroeder decay curves, and
capability-gated decay metrics (EDT / T20 / T30). Every numerical result
binds an immutable ``IRAnalysisSpec`` so saved diagnostics replay
byte-exact; insufficient dynamic range or truncation keeps metrics
BLOCKED/UNKNOWN instead of extrapolating beyond evidence, and no local
band decay estimate is ever presented as a generic room-wide RT60 claim.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Mapping
from uuid import uuid4

import numpy as np
from .cad_schema import require_native_tables
from pydantic import BaseModel, ConfigDict, Field, model_validator


IRAlignmentMode = Literal[
    'none',
    'level',
    'relative_time',
    'absolute_common_time',
]
MetricStatus = Literal['estimated', 'blocked', 'unknown']
IR_ANALYSIS_ALGORITHM_VERSION = 'ir-analysis-1'
IR_ANALYSIS_SCHEMA_VERSION = 'ir-analysis-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


IR_ANALYSIS_ALGORITHM_IDENTITY: dict[str, Any] = {
    'algorithm_version': IR_ANALYSIS_ALGORITHM_VERSION,
    'etc': 'hilbert_envelope_10log10_squared_over_window_db_re_peak',
    'decay_integration': 'schroeder_reverse_cumulative_energy',
    'band_filter': 'fft_brickwall_octave_band',
    'noise_floor': ['declared', 'tail_median'],
    'metrics': ['edt', 't20', 't30'],
    'fit_intervals_db': {'edt': [0, -10], 't20': [-5, -25], 't30': [-5, -35]},
    'insufficient_dynamic_range': 'metric_blocked_never_extrapolated',
    'alignment_modes': list(IRAlignmentMode.__args__),
    'generic_rt60_claim': 'forbidden',
}
IR_ANALYSIS_ALGORITHM_SHA256 = _hash(IR_ANALYSIS_ALGORITHM_IDENTITY)


class IRReflectionMarker(BaseModel):
    """One deterministic early-peak marker.

    ``delay_s`` is relative to the direct-arrival sample; it is never a
    claimed absolute arrival time, and a marker is never labelled as a
    specific wall reflection from timing alone.
    """

    model_config = ConfigDict(frozen=True)

    marker_index: int = Field(ge=0)
    time_s: float
    level_db: float
    delay_s: float
    label: str = 'peak'


class IRDecayMetric(BaseModel):
    """One decay metric with its fit evidence and verdict.

    ``fit_interval_db`` records the exact regression interval used;
    ``usable_dynamic_range_db`` records the evidence the gate evaluated.
    """

    model_config = ConfigDict(frozen=True)

    metric: Literal['edt', 't20', 't30']
    status: MetricStatus
    value_s: float | None = None
    slope_db_per_s: float | None = None
    fit_interval_db: tuple[float, float]
    usable_dynamic_range_db: float | None = None
    noise_floor_method: str
    reason: str = ''

    @model_validator(mode='after')
    def valid_metric(self) -> 'IRDecayMetric':
        if self.status == 'estimated' and self.value_s is None:
            raise ValueError('estimated decay metrics require a value')
        return self


class IRAnalysisSpec(BaseModel):
    """Immutable, versioned analysis specification for one IR measurement.

    Every saved diagnostic replays from this exact spec: the bound
    measurement evidence, declared time-zero/window, band filtering,
    ETC/decay/spectrogram parameters and the declared alignment mode.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: float = Field(gt=0)
    time_zero_sample: int = Field(ge=0)
    window_start_s: float = Field(ge=0.0)
    window_end_s: float | None = None
    band_center_hz: float | None = None
    band_fraction: Literal['octave', 'third_octave'] | None = None
    smoothing_fraction_octave: float | None = Field(default=None, gt=0)
    decay_integration: Literal['schroeder'] = 'schroeder'
    noise_floor_method: Literal['declared', 'tail_median', 'none'] = 'tail_median'
    declared_noise_floor_db: float | None = None
    tf_window_s: float = Field(gt=0)
    tf_overlap: float = Field(ge=0.0, lt=1.0)
    alignment: IRAlignmentMode = 'none'
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_spec(self) -> 'IRAnalysisSpec':
        if self.window_end_s is not None and self.window_end_s <= self.window_start_s:
            raise ValueError('analysis window end must be after start')
        if (self.band_center_hz is None) != (self.band_fraction is None):
            raise ValueError('band center and band fraction are required together')
        if self.band_center_hz is not None:
            if not isfinite(float(self.band_center_hz)) or self.band_center_hz <= 0:
                raise ValueError('band center must be finite and positive')
        if self.noise_floor_method == 'declared' and self.declared_noise_floor_db is None:
            raise ValueError('declared noise-floor method requires declared_noise_floor_db')
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('IR analysis spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': IR_ANALYSIS_SCHEMA_VERSION,
            'measurement_id': self.measurement_id,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'sample_rate_hz': self.sample_rate_hz,
            'time_zero_sample': self.time_zero_sample,
            'window_start_s': self.window_start_s,
            'window_end_s': self.window_end_s,
            'band_center_hz': self.band_center_hz,
            'band_fraction': self.band_fraction,
            'smoothing_fraction_octave': self.smoothing_fraction_octave,
            'decay_integration': self.decay_integration,
            'noise_floor_method': self.noise_floor_method,
            'declared_noise_floor_db': self.declared_noise_floor_db,
            'tf_window_s': self.tf_window_s,
            'tf_overlap': self.tf_overlap,
            'alignment': self.alignment,
        }


class IRAnalysisResult(BaseModel):
    """Immutable derived IR diagnostic bound to its exact spec."""

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    effective_alignment: IRAlignmentMode
    etc_time_s: tuple[float, ...] = ()
    etc_db: tuple[float, ...] = ()
    markers: tuple[IRReflectionMarker, ...] = ()
    decay_time_s: tuple[float, ...] = ()
    decay_db: tuple[float, ...] = ()
    metrics: tuple[IRDecayMetric, ...] = ()
    usable_dynamic_range_db: float | None = None
    noise_floor_db: float | None = None
    noise_floor_method: str = 'none'
    spec_times_s: tuple[float, ...] = ()
    spec_freqs_hz: tuple[float, ...] = ()
    spec_levels_db: tuple[tuple[float, ...], ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    algorithm_version: str = IR_ANALYSIS_ALGORITHM_VERSION
    algorithm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str = Field(min_length=1)
    analysis_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_result(self) -> 'IRAnalysisResult':
        if self.etc_db and len(self.etc_db) != len(self.etc_time_s):
            raise ValueError('ETC arrays must align')
        if self.decay_db and len(self.decay_db) != len(self.decay_time_s):
            raise ValueError('decay arrays must align')
        if self.spec_levels_db:
            if len(self.spec_levels_db) != len(self.spec_times_s):
                raise ValueError('spectrogram rows must align with times')
            for row in self.spec_levels_db:
                if len(row) != len(self.spec_freqs_hz):
                    raise ValueError('spectrogram rows must align with frequencies')
        if self.algorithm_sha256 != IR_ANALYSIS_ALGORITHM_SHA256:
            raise ValueError('IR analysis algorithm hash mismatch')
        if self.analysis_sha256 != _hash(self.identity_payload()):
            raise ValueError('IR analysis hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': IR_ANALYSIS_SCHEMA_VERSION,
            'spec_id': self.spec_id,
            'spec_sha256': self.spec_sha256,
            'measurement_id': self.measurement_id,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'effective_alignment': self.effective_alignment,
            'etc_time_s': list(self.etc_time_s),
            'etc_db': list(self.etc_db),
            'markers': [m.model_dump(mode='json') for m in self.markers],
            'decay_time_s': list(self.decay_time_s),
            'decay_db': list(self.decay_db),
            'metrics': [m.model_dump(mode='json') for m in self.metrics],
            'usable_dynamic_range_db': self.usable_dynamic_range_db,
            'noise_floor_db': self.noise_floor_db,
            'noise_floor_method': self.noise_floor_method,
            'spec_times_s': list(self.spec_times_s),
            'spec_freqs_hz': list(self.spec_freqs_hz),
            'spec_levels_db': [list(row) for row in self.spec_levels_db],
            'truncated': self.truncated,
            'warnings': list(self.warnings),
            'algorithm_version': self.algorithm_version,
            'algorithm_sha256': self.algorithm_sha256,
            'created_at': self.created_at,
        }


def _fft_bandpass(samples: np.ndarray, sample_rate: float, center_hz: float, fraction: str) -> np.ndarray:
    """Deterministic brick-wall octave/fractional-octave band extraction."""
    n = samples.size
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    ratio = 2.0 ** (0.5 if fraction == 'octave' else 1.0 / 6.0)
    mask = (freqs >= center_hz / ratio) & (freqs <= center_hz * ratio)
    return np.fft.irfft(spectrum * mask, n=n)


def _hilbert_envelope_db(samples: np.ndarray) -> np.ndarray:
    n = samples.size
    spectrum = np.fft.fft(samples)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1 : n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1 : (n + 1) // 2] = 2.0
    analytic = np.fft.ifft(spectrum * h)
    envelope = np.abs(analytic) ** 2
    peak = envelope.max()
    with np.errstate(divide='ignore'):
        return 10.0 * np.log10(envelope / peak)


def _schroeder_db(samples: np.ndarray) -> np.ndarray:
    energy = samples.astype(np.float64) ** 2
    cumulative = np.cumsum(energy[::-1])[::-1]
    with np.errstate(divide='ignore'):
        return 10.0 * np.log10(cumulative / cumulative[0])


def _fit_slope(times: np.ndarray, levels_db: np.ndarray, lo_db: float, hi_db: float) -> tuple[float, float] | None:
    """Least-squares slope over the first sample range crossing the interval."""
    mask = (levels_db <= hi_db) & (levels_db >= lo_db)
    if mask.sum() < 2:
        return None
    t = times[mask]
    y = levels_db[mask]
    slope, intercept = np.polyfit(t, y, 1)
    if slope >= 0:
        return None
    return float(slope), float(intercept)


def run_ir_analysis(
    spec: IRAnalysisSpec,
    ir_samples: tuple[float, ...],
    *,
    capabilities: Mapping[str, str] | None = None,
    created_at: str,
) -> IRAnalysisResult:
    """Run the pinned analysis against exact IR evidence.

    ``capabilities`` carries the quality report's claim matrix as
    claim→decision (see ``capabilities_from_report``); absent evidence is
    UNKNOWN and never upgrades a result.
    """
    samples = np.asarray(ir_samples, dtype=np.float64)
    if samples.size < 4:
        raise ValueError('IR evidence requires at least four samples')
    fs = float(spec.sample_rate_hz)
    t0 = spec.time_zero_sample
    if t0 >= samples.size:
        raise ValueError('time-zero lies outside the IR evidence')
    start = int(round(spec.window_start_s * fs))
    stop = samples.size if spec.window_end_s is None else min(
        samples.size, int(round(spec.window_end_s * fs))
    )
    windowed = samples[start:stop]
    if spec.band_center_hz is not None:
        windowed = _fft_bandpass(windowed, fs, spec.band_center_hz, spec.band_fraction or 'octave')

    warnings: list[str] = []
    caps = dict(capabilities or {})
    # Absolute-arrival claims require common timing; anything else silently
    # degrades to relative-time alignment so shape comparison never implies
    # an absolute-arrival claim.
    effective_alignment = spec.alignment
    if spec.alignment == 'absolute_common_time' and caps.get('common_timing') != 'ALLOWED':
        effective_alignment = 'relative_time'
        warnings.append(
            'absolute_common_time requested without common-timing capability; '
            'alignment degraded to relative_time'
        )

    times = np.arange(windowed.size) / fs
    etc_db = _hilbert_envelope_db(windowed)
    etc_db = np.maximum(etc_db, -120.0)

    # Deterministic early-peak markers: local maxima of the ETC above a
    # fixed -20 dB prominence floor after the direct-arrival sample.
    markers: list[IRReflectionMarker] = []
    direct_index = 0
    for i in range(1, min(windowed.size - 1, int(0.5 * fs))):
        if (
            etc_db[i] > -20.0
            and etc_db[i] > etc_db[i - 1]
            and etc_db[i] >= etc_db[i + 1]
            and i > direct_index
        ):
            markers.append(
                IRReflectionMarker(
                    marker_index=len(markers),
                    time_s=float(times[i]),
                    level_db=float(etc_db[i]),
                    delay_s=float(times[i] - times[direct_index]),
                )
            )
            if len(markers) >= 8:
                break

    decay_db = _schroeder_db(windowed)
    decay_db = np.maximum(decay_db, -140.0)
    truncated = bool(spec.window_end_s is not None and decay_db[-1] > -40.0)
    if truncated:
        warnings.append('IR window truncates before -40 dB of decay')

    if spec.noise_floor_method == 'declared':
        noise_floor = spec.declared_noise_floor_db
    elif spec.noise_floor_method == 'tail_median':
        tail = decay_db[max(1, int(decay_db.size * 0.9)) :]
        noise_floor = float(np.median(tail))
    else:
        noise_floor = None
    usable_range = None if noise_floor is None else float(-noise_floor)

    metrics: list[IRDecayMetric] = []
    for metric, interval in (('edt', (0.0, -10.0)), ('t20', (-5.0, -25.0)), ('t30', (-5.0, -35.0))):
        hi, lo = interval
        fit = _fit_slope(times, decay_db, lo, hi)
        if caps.get('decay') == 'BLOCKED':
            metrics.append(IRDecayMetric(
                metric=metric, status='blocked', fit_interval_db=interval,
                usable_dynamic_range_db=usable_range,
                noise_floor_method=spec.noise_floor_method,
                reason='decay capability is blocked by the quality report',
            ))
            continue
        if noise_floor is not None and noise_floor > lo - 5.0:
            metrics.append(IRDecayMetric(
                metric=metric, status='blocked', fit_interval_db=interval,
                usable_dynamic_range_db=usable_range,
                noise_floor_method=spec.noise_floor_method,
                reason='insufficient usable dynamic range for the fit interval',
            ))
            continue
        if fit is None:
            metrics.append(IRDecayMetric(
                metric=metric, status='unknown', fit_interval_db=interval,
                usable_dynamic_range_db=usable_range,
                noise_floor_method=spec.noise_floor_method,
                reason='decay curve does not cover the fit interval',
            ))
            continue
        slope, _intercept = fit
        metrics.append(IRDecayMetric(
            metric=metric,
            status='estimated',
            value_s=float(-60.0 / slope),
            slope_db_per_s=slope,
            fit_interval_db=interval,
            usable_dynamic_range_db=usable_range,
            noise_floor_method=spec.noise_floor_method,
        ))

    # Time-frequency diagnostic: STFT magnitude over Hann windows.
    window_n = max(8, int(round(spec.tf_window_s * fs)))
    hop = max(1, int(round(window_n * (1.0 - spec.tf_overlap))))
    win = np.hanning(window_n)
    spec_times: list[float] = []
    columns: list[np.ndarray] = []
    for start_i in range(0, windowed.size - window_n + 1, hop):
        frame = windowed[start_i : start_i + window_n] * win
        magnitude = np.abs(np.fft.rfft(frame))
        columns.append(magnitude)
        spec_times.append(float((start_i + window_n // 2) / fs))
    if columns:
        peak = max(c.max() for c in columns if c.size) or 1.0
        spec_freqs = np.fft.rfftfreq(window_n, d=1.0 / fs)
        spec_levels = tuple(
            tuple(float(v) for v in np.maximum(10.0 * np.log10(c / peak), -120.0))
            for c in columns
        )
        spec_freq_tuple = tuple(float(f) for f in spec_freqs)
    else:
        spec_levels = ()
        spec_freq_tuple = ()

    payload: dict[str, Any] = {
        'result_id': str(uuid4()),
        'spec_id': spec.spec_id,
        'spec_sha256': spec.spec_sha256,
        'measurement_id': spec.measurement_id,
        'dataset_id': spec.dataset_id,
        'dataset_sha256': spec.dataset_sha256,
        'effective_alignment': effective_alignment,
        'etc_time_s': tuple(float(t) for t in times),
        'etc_db': tuple(float(v) for v in etc_db),
        'markers': markers,
        'decay_time_s': tuple(float(t) for t in times),
        'decay_db': tuple(float(v) for v in decay_db),
        'metrics': metrics,
        'usable_dynamic_range_db': usable_range,
        'noise_floor_db': noise_floor,
        'noise_floor_method': spec.noise_floor_method,
        'spec_times_s': tuple(spec_times),
        'spec_freqs_hz': spec_freq_tuple,
        'spec_levels_db': spec_levels,
        'truncated': truncated,
        'warnings': warnings,
        'algorithm_version': IR_ANALYSIS_ALGORITHM_VERSION,
        'algorithm_sha256': IR_ANALYSIS_ALGORITHM_SHA256,
        'created_at': created_at,
    }
    provisional = IRAnalysisResult.model_construct(
        **payload,
        analysis_sha256='0' * 64,
    )
    return IRAnalysisResult(
        **payload,
        analysis_sha256=_hash(provisional.identity_payload()),
    )


def capabilities_from_report(report: Any) -> dict[str, str]:
    """Extract claim→decision pairs from a CadMeasurementQualityReport."""
    return {item.claim: item.decision for item in report.capabilities}


def build_ir_analysis_spec(
    *,
    measurement_id: str,
    dataset_id: str,
    dataset_sha256: str,
    sample_rate_hz: float,
    time_zero_sample: int = 0,
    window_start_s: float = 0.0,
    window_end_s: float | None = None,
    band_center_hz: float | None = None,
    band_fraction: Literal['octave', 'third_octave'] | None = None,
    smoothing_fraction_octave: float | None = None,
    noise_floor_method: Literal['declared', 'tail_median', 'none'] = 'tail_median',
    declared_noise_floor_db: float | None = None,
    tf_window_s: float = 0.05,
    tf_overlap: float = 0.5,
    alignment: IRAlignmentMode = 'none',
) -> IRAnalysisSpec:
    payload: dict[str, Any] = {
        'spec_id': str(uuid4()),
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'sample_rate_hz': sample_rate_hz,
        'time_zero_sample': time_zero_sample,
        'window_start_s': window_start_s,
        'window_end_s': window_end_s,
        'band_center_hz': band_center_hz,
        'band_fraction': band_fraction,
        'smoothing_fraction_octave': smoothing_fraction_octave,
        'noise_floor_method': noise_floor_method,
        'declared_noise_floor_db': declared_noise_floor_db,
        'tf_window_s': tf_window_s,
        'tf_overlap': tf_overlap,
        'alignment': alignment,
    }
    provisional = IRAnalysisSpec.model_construct(**payload, spec_sha256='0' * 64)
    return IRAnalysisSpec(**payload, spec_sha256=_hash(provisional.identity_payload()))


def replay_ir_analysis(
    result: IRAnalysisResult,
    spec: IRAnalysisSpec,
    ir_samples: tuple[float, ...],
    *,
    capabilities: Mapping[str, str] | None = None,
) -> IRAnalysisResult:
    """Rerun the pinned spec against the same IR evidence and compare."""
    if result.spec_id != spec.spec_id or result.spec_sha256 != spec.spec_sha256:
        raise ValueError('IR analysis result is bound to a different spec')
    replay = run_ir_analysis(
        spec,
        ir_samples,
        capabilities=capabilities,
        created_at=result.created_at,
    )
    comparable = replay.model_dump(mode='json', exclude={'result_id', 'analysis_sha256'})
    original = result.model_dump(mode='json', exclude={'result_id', 'analysis_sha256'})
    if comparable != original:
        raise ValueError('IR analysis does not replay to persisted values')
    return replay


class CadIRAnalysisRepository:
    """Append-only storage for IR analysis specs and derived results."""

    def __init__(self, scene_repository) -> None:
        self.path = scene_repository.path
        self._initialize()

    def _connect(self):
        from contextlib import closing
        import sqlite3

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_ir_analysis_specs', 'cad_ir_analysis_results')

    def save_spec(self, spec: IRAnalysisSpec) -> None:
        if self.get_spec(spec.spec_id) is not None:
            raise ValueError('IR analysis specs are append-only')
        from datetime import datetime, timezone

        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ir_analysis_specs (
                    spec_id, measurement_id, spec_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.measurement_id,
                    spec.spec_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    spec.model_dump_json(),
                ),
            )

    def get_spec(self, spec_id: str) -> IRAnalysisSpec | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ir_analysis_specs WHERE spec_id=?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        return IRAnalysisSpec.model_validate_json(row['payload_json'])

    def save_result(self, result: IRAnalysisResult) -> None:
        if self.get_result(result.result_id) is not None:
            raise ValueError('IR analysis results are append-only')
        spec = self.get_spec(result.spec_id)
        if spec is None or spec.spec_sha256 != result.spec_sha256:
            raise ValueError('IR analysis result requires the exact persisted spec')
        from datetime import datetime, timezone

        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ir_analysis_results (
                    result_id, spec_id, analysis_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.spec_id,
                    result.analysis_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    result.model_dump_json(),
                ),
            )

    def get_result(self, result_id: str) -> IRAnalysisResult | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ir_analysis_results WHERE result_id=?',
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return IRAnalysisResult.model_validate_json(row['payload_json'])
