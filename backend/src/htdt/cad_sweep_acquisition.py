"""HTDT-native automated sweep measurement engine (#869, REV66).

Acoustic sweep measurement belongs in HTDT itself: generate the sweep,
play and record it through a pluggable audio backend, derive the impulse
response, and gate the result through an explicit quality evaluation —
all while retaining sealed evidence for every claim. No external tool's
paid automation API is part of the acquisition path.

Provider-neutral core:
- SweepStimulusGenerator: deterministic exponential log-sine stimulus,
  hashed from parameters plus the generator version.
- AudioIOBackend: device enumeration/routing/rate binding contract with
  no silent fallback. ``FakeAudioBackend`` deterministically simulates
  latency, drift, clipping, xruns, truncation, noise, cancellation and
  device loss; ``WasapiAudioBackend`` reports backend_unavailable until a
  device-capable implementation lands (fail closed, never pretend).
- TimingReferenceResolver: loopback / declared-topology / onset evidence
  with an explicit UNKNOWN/limited path — timing is never silently
  aligned into validity.
- ImpulseResponseDeriver: versioned inverse-sweep deconvolution plus
  latency alignment, IR windowing, normalization and optional frequency
  response derivation.
- Quality gate: distinct verdict reasons for clipping, low SNR,
  truncation, xruns, calibration problems, unusable synchronization,
  excessive noise and inconsistent repetitions. A completed recording is
  not automatically valid.
- MeasurementAcquisitionEngine: the PRECHECK -> READY -> ARMED ->
  PLAYING/RECORDING -> PROCESSING -> QUALITY_CHECK -> terminal state
  machine with explicit transition reasons, first-class cancel, and
  armed-state invalidation on configuration change.
- Level-safety: the operator must see the device/channel/level that is
  armed and explicitly confirm it; a configurable level limit refuses
  arming when exceeded.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .clock import utc_now_iso as _utc_now
from .canonical_json import canonical_sha256, canonicalize_payload


# ---------------------------------------------------------------------------
# Version pins — a change to any algorithm here bumps the version string so
# historical evidence keeps its own derivation identity.
# ---------------------------------------------------------------------------

SWEEP_GENERATOR_VERSION = 'htdt-sweep-gen-1'
IR_DERIVATION_VERSION = 'htdt-ir-derive-1'
SWEEP_ACQUISITION_ENGINE_VERSION = 'htdt-acq-engine-1'
SWEEP_ACQUISITION_SCHEMA_VERSION = 'rev66-869-1'

_FAKE_BACKEND_ID = 'fake-audio-io'
_FAKE_BACKEND_VERSION = 'fake-audio-io-1'
_WASAPI_BACKEND_ID = 'wasapi-audio-io'
_WASAPI_BACKEND_VERSION = 'wasapi-unavailable-1'

# ---------------------------------------------------------------------------
# Errors — every failure the engine can raise is one of these so the error
# boundary can render operator-facing text deterministically.
# ---------------------------------------------------------------------------


class SweepAcquisitionError(ValueError):
    """Base class for acquisition-engine failures.

    A ValueError subclass like every other domain rejection, so the UI
    error boundary treats it as an expected operational failure.
    """


class AcquisitionStateError(SweepAcquisitionError):
    """Illegal state transition or stale armed state."""


class ArmBlockedError(SweepAcquisitionError):
    """The level-safety / configuration gate refused arming."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__('; '.join(self.reasons))


class BackendUnavailableError(SweepAcquisitionError):
    """The backend cannot run on this host at all."""


class DeviceNotFoundError(SweepAcquisitionError):
    """A requested device id is not enumerated by the backend."""


class UnsupportedConfigurationError(SweepAcquisitionError):
    """Requested rate/format/channel plan is not supported — no fallback."""


# ---------------------------------------------------------------------------
# Sweep stimulus specification + generator
# ---------------------------------------------------------------------------


class SweepStimulusSpec(BaseModel):
    """Operator-facing stimulus parameters.

    Everything about the waveform derives deterministically from these
    fields plus SWEEP_GENERATOR_VERSION, so identical specs always hash
    identically.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    start_frequency_hz: float = Field(gt=0.0)
    end_frequency_hz: float = Field(gt=0.0)
    duration_s: float = Field(gt=0.0, le=120.0)
    level_dbfs: float = Field(le=0.0)
    sample_rate_hz: int = Field(gt=0)
    pre_roll_s: float = Field(ge=0.0, default=0.05)
    post_roll_s: float = Field(ge=0.0, default=0.05)
    fade_in_s: float = Field(ge=0.0, default=0.005)
    fade_out_s: float = Field(ge=0.0, default=0.005)
    repetitions: int = Field(ge=1, le=64, default=1)
    repetition_gap_s: float = Field(ge=0.0, default=0.1)

    def validate_geometry(self) -> tuple[str, ...]:
        """Non-pydantic ordering checks the model itself cannot express."""

        problems: list[str] = []
        if self.end_frequency_hz <= self.start_frequency_hz:
            problems.append('end_frequency_hz must exceed start_frequency_hz')
        if self.end_frequency_hz >= self.sample_rate_hz / 2.0:
            problems.append('end_frequency_hz must be below Nyquist')
        if self.fade_in_s + self.fade_out_s > self.duration_s:
            problems.append('fades must fit inside duration_s')
        return tuple(problems)


@dataclass(frozen=True)
class GeneratedStimulus:
    """A rendered stimulus block: samples plus identity hashes."""

    spec: SweepStimulusSpec
    generator_version: str
    samples: tuple[float, ...]  # one repetition block: pre-roll + sweep + post-roll
    sweep_start_sample: int     # index of the sweep inside the block
    sweep_end_sample: int       # one past the last sweep sample
    params_sha256: str          # hash of spec + generator version
    samples_sha256: str         # hash of the float64 sample bytes

    @property
    def block_frames(self) -> int:
        return len(self.samples)

    @property
    def sweep_samples(self) -> int:
        return self.sweep_end_sample - self.sweep_start_sample


def _sweep_samples(spec: SweepStimulusSpec) -> np.ndarray:
    """Exponential sine sweep (Farina/ESS law), deterministic float64."""

    n = int(round(spec.duration_s * spec.sample_rate_hz))
    if n < 2:
        return np.zeros(n, dtype=np.float64)
    t = np.arange(n, dtype=np.float64) / spec.sample_rate_hz
    f1 = spec.start_frequency_hz
    f2 = spec.end_frequency_hz
    k = math.log(f2 / f1)
    phase = 2.0 * math.pi * f1 * (spec.duration_s / k) * np.expm1(t * k / spec.duration_s)
    wave = np.sin(phase)
    amplitude = math.pow(10.0, spec.level_dbfs / 20.0)
    wave = wave * amplitude
    fade_in = int(round(spec.fade_in_s * spec.sample_rate_hz))
    fade_out = int(round(spec.fade_out_s * spec.sample_rate_hz))
    if fade_in > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade_in) / fade_in)
        wave[:fade_in] *= ramp
    if fade_out > 0:
        ramp = 0.5 + 0.5 * np.cos(np.pi * np.arange(fade_out) / fade_out)
        wave[n - fade_out:] *= ramp
    return np.asarray(wave, dtype=np.float64)


def _samples_sha256(samples: np.ndarray) -> str:
    return canonical_sha256({'pcm_f64le_hex': np.ascontiguousarray(
        samples.astype('<f8'), dtype='<f8').tobytes().hex()})


def _stimulus_params_sha256(spec: SweepStimulusSpec, generator_version: str) -> str:
    return canonical_sha256({
        'generator_version': generator_version,
        'start_frequency_hz': spec.start_frequency_hz,
        'end_frequency_hz': spec.end_frequency_hz,
        'duration_s': spec.duration_s,
        'level_dbfs': spec.level_dbfs,
        'sample_rate_hz': spec.sample_rate_hz,
        'pre_roll_s': spec.pre_roll_s,
        'post_roll_s': spec.post_roll_s,
        'fade_in_s': spec.fade_in_s,
        'fade_out_s': spec.fade_out_s,
    })


class SweepStimulusGenerator:
    """Renders deterministic log-sine stimulus blocks.

    generate() is pure: the same spec always produces byte-identical
    samples and the same params/samples hashes.
    """

    def __init__(self, generator_version: str = SWEEP_GENERATOR_VERSION):
        self.generator_version = generator_version

    def generate(self, spec: SweepStimulusSpec) -> GeneratedStimulus:
        problems = spec.validate_geometry()
        if problems:
            raise UnsupportedConfigurationError('; '.join(problems))
        pre = int(round(spec.pre_roll_s * spec.sample_rate_hz))
        post = int(round(spec.post_roll_s * spec.sample_rate_hz))
        sweep = _sweep_samples(spec)
        block = np.concatenate([
            np.zeros(pre, dtype=np.float64), sweep,
            np.zeros(post, dtype=np.float64),
        ])
        return GeneratedStimulus(
            spec=spec,
            generator_version=self.generator_version,
            samples=tuple(float(x) for x in block),
            sweep_start_sample=pre,
            sweep_end_sample=pre + len(sweep),
            params_sha256=_stimulus_params_sha256(spec, self.generator_version),
            samples_sha256=_samples_sha256(block),
        )


# ---------------------------------------------------------------------------
# Audio backend contract
# ---------------------------------------------------------------------------


class AudioDeviceInfo(BaseModel):
    """One enumerated audio device as the backend reports it."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_id: str
    display_name: str
    direction: Literal['playback', 'capture', 'duplex']
    max_output_channels: int = Field(ge=0, default=0)
    max_input_channels: int = Field(ge=0, default=0)
    supported_sample_rates: tuple[int, ...] = ()
    supported_formats: tuple[str, ...] = ()
    is_default: bool = False


class ChannelRouting(BaseModel):
    """Explicit device/channel binding — never inferred."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    playback_device_id: str
    playback_channel: int = Field(ge=0)
    capture_device_id: str
    capture_channel: int = Field(ge=0)
    # Optional wired loopback reference channel on the capture device.
    loopback_input_channel: int | None = Field(ge=0, default=None)


class AudioStreamConfig(BaseModel):
    """Requested stream parameters; the backend reports what it ACTUALLY
    bound — requested-vs-actual is retained as evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    sample_rate_hz: int = Field(gt=0)
    sample_format: str = 'float64'
    routing: ChannelRouting


CaptureOutcome = Literal['completed', 'cancelled', 'device_lost']


class CaptureEvent(BaseModel):
    """A backend-reported capture anomaly."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['xrun', 'underflow', 'overflow', 'clip', 'drift', 'device_event']
    at_frame: int = Field(ge=0)
    detail: str = ''


class CaptureResult(BaseModel):
    """What one playback+record run produced. Honest about everything the
    backend observed; multi-channel samples are frame-major rows."""

    model_config = ConfigDict(frozen=True, extra='forbid', arbitrary_types_allowed=True)

    outcome: CaptureOutcome
    actual_sample_rate_hz: int
    actual_sample_format: str
    captured_channels: tuple[int, ...]  # device channel indices actually recorded
    samples: Any  # np.ndarray (frames, len(captured_channels)) float64
    expected_frames: int
    recorded_frames: int
    truncated: bool = False
    xrun_count: int = 0
    clipped_samples: int = 0
    events: tuple[CaptureEvent, ...] = ()

    def channel_samples(self, channel_position: int) -> np.ndarray:
        return np.asarray(self.samples, dtype=np.float64)[:, channel_position]


class AcquisitionStream:
    """A bound playback+record session returned by AudioIOBackend.open_stream."""

    def run_acquisition(
        self,
        stimulus_block: np.ndarray,
        repetitions: int,
        gap_samples: int,
        should_cancel: Callable[[], bool],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> CaptureResult:
        raise NotImplementedError


class AudioIOBackend:
    """The acquisition engine's audio path contract.

    Rules: enumerate honestly (empty is a legal answer), bind exactly the
    requested device/channel/rate or fail deterministically, never
    silently substitute a device/rate/channel.
    """

    backend_id = 'abstract'
    backend_version = 'abstract-0'

    def enumerate_devices(self) -> tuple[AudioDeviceInfo, ...]:
        raise NotImplementedError

    def open_stream(self, config: AudioStreamConfig) -> AcquisitionStream:
        raise NotImplementedError


class WasapiAudioBackend(AudioIOBackend):
    """Windows WASAPI backend — stubbed.

    Real device I/O is untestable on this box, so this backend reports
    itself unavailable and fails closed. Everything the engine records
    about it says exactly that.
    """

    backend_id = _WASAPI_BACKEND_ID
    backend_version = _WASAPI_BACKEND_VERSION

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return 'wasapi backend is not implemented on this build (device i/o untestable)'

    def enumerate_devices(self) -> tuple[AudioDeviceInfo, ...]:
        return ()

    def open_stream(self, config: AudioStreamConfig) -> AcquisitionStream:
        raise BackendUnavailableError(self.unavailable_reason())


# ---------------------------------------------------------------------------
# Fake backend — deterministic simulation of every named failure mode.
# Test evidence only: its simulated completion is NOT a real capture.
# ---------------------------------------------------------------------------

_IR_TAP = tuple[int, float]  # (sample offset, gain)


class FakeBackendScenario(BaseModel):
    """Everything the fake backend will simulate — all deterministic."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    devices: tuple[AudioDeviceInfo, ...] = ()
    electrical_latency_samples: int = Field(ge=0, default=64)  # loopback delay
    acoustic_latency_samples: int = Field(ge=0, default=96)    # measurement path delay
    drift_ppm: float = 0.0                     # per-repetition resampling drift
    noise_rms_dbfs: float | None = None        # additive gaussian noise floor
    clip_at_dbfs: float | None = None          # hard-clip the measurement channel
    xrun_at_rep_frame: tuple[tuple[int, int], ...] = ()  # (rep, frame) zero-span starts
    xrun_span_frames: int = Field(ge=0, default=0)
    truncate_at_frame: int | None = None       # early 'completed' stop → truncated
    device_loss_at_frame: int | None = None    # mid-run loss → outcome device_lost
    measurement_ir_taps: tuple[_IR_TAP, ...] = ((0, 1.0),)  # acoustic response
    measurement_gain_db: float = 0.0           # extra attenuation → SNR control
    noise_seed: int = 1337
    cancel_after_frames: int | None = None     # engine cancel observed mid-run


def default_fake_devices() -> tuple[AudioDeviceInfo, ...]:
    return (
        AudioDeviceInfo(
            device_id='fake-duplex-0',
            display_name='Fake Duplex Device 0',
            direction='duplex',
            max_output_channels=2,
            max_input_channels=4,
            supported_sample_rates=(44100, 48000, 96000),
            supported_formats=('float64', 'float32'),
            is_default=True,
        ),
    )


def default_fake_scenario(**overrides: Any) -> FakeBackendScenario:
    devices = overrides.pop('devices', default_fake_devices())
    return FakeBackendScenario(devices=tuple(devices), **overrides)


class _FakeStream(AcquisitionStream):
    def __init__(self, backend: 'FakeAudioBackend', config: AudioStreamConfig):
        self._backend = backend
        self._config = config

    def run_acquisition(
        self,
        stimulus_block: np.ndarray,
        repetitions: int,
        gap_samples: int,
        should_cancel: Callable[[], bool],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> CaptureResult:
        sc = self._backend.scenario
        rate = self._config.sample_rate_hz
        routing = self._config.routing
        block = np.asarray(stimulus_block, dtype=np.float64)
        gap = np.zeros(gap_samples, dtype=np.float64)

        # Measurement channel impulse response (acoustic path).
        ir_len = 1 + max((t[0] for t in sc.measurement_ir_taps), default=0)
        ir = np.zeros(ir_len, dtype=np.float64)
        for off, gain in sc.measurement_ir_taps:
            ir[off] += gain

        n_capture_channels = 1 + (1 if routing.loopback_input_channel is not None else 0)
        expected_frames = repetitions * (len(block) + gap_samples) - gap_samples
        chunks: list[np.ndarray] = []
        events: list[CaptureEvent] = []
        xruns = 0
        frames_done = 0
        outcome: CaptureOutcome = 'completed'
        rng = np.random.default_rng(sc.noise_seed)
        noise_rms = (math.pow(10.0, sc.noise_rms_dbfs / 20.0)
                     if sc.noise_rms_dbfs is not None else 0.0)
        gain = math.pow(10.0, sc.measurement_gain_db / 20.0)
        xrun_map = {rep: frame for rep, frame in sc.xrun_at_rep_frame}
        clip_amp = (math.pow(10.0, sc.clip_at_dbfs / 20.0)
                    if sc.clip_at_dbfs is not None else None)
        clipped_total = 0

        for rep in range(repetitions):
            if frames_done >= expected_frames or outcome != 'completed':
                break
            if should_cancel() or (
                sc.cancel_after_frames is not None
                and frames_done >= sc.cancel_after_frames
            ):
                outcome = 'cancelled'
                break
            rep_block = block
            if sc.drift_ppm and rep > 0:
                # Deterministic clock drift: resample this rep's stimulus.
                factor = 1.0 + sc.drift_ppm * rep / 1e6
                n_new = max(2, int(round(len(block) * factor)))
                rep_block = np.interp(
                    np.linspace(0.0, len(block) - 1.0, n_new),
                    np.arange(len(block)), block,
                )
            # Measurement channel: convolve + acoustic latency.
            meas = np.convolve(rep_block, ir)[: len(rep_block)] * gain
            meas = np.concatenate([
                np.zeros(sc.acoustic_latency_samples), meas,
            ])[: len(rep_block)]
            # Loopback channel: electrical copy at electrical latency.
            if n_capture_channels == 2:
                loop = np.concatenate([
                    np.zeros(sc.electrical_latency_samples), rep_block,
                ])[: len(rep_block)]
            # Noise on the measurement channel.
            if noise_rms > 0.0:
                meas = meas + rng.normal(0.0, noise_rms, size=len(meas))
            # Simulated xrun: zero a span mid-rep (discontinuity).
            if rep in xrun_map and sc.xrun_span_frames > 0:
                start = min(xrun_map[rep], len(meas) - 1)
                end = min(start + sc.xrun_span_frames, len(meas))
                meas[start:end] = 0.0
                xruns += 1
                events.append(CaptureEvent(
                    kind='xrun', at_frame=frames_done + start,
                    detail=f'simulated dropout rep={rep}',
                ))
            if n_capture_channels == 2 and noise_rms > 0.0:
                loop = loop + rng.normal(0.0, noise_rms * 0.1, size=len(loop))
            # Clipping on the measurement channel only.
            if clip_amp is not None:
                over = np.abs(meas) > clip_amp
                clipped_total += int(np.count_nonzero(over))
                if np.any(over):
                    meas = np.clip(meas, -clip_amp, clip_amp)
                    events.append(CaptureEvent(
                        kind='clip', at_frame=frames_done,
                        detail='simulated clipping',
                    ))
            rep_out = np.stack(
                [meas] if n_capture_channels == 1 else [meas, loop], axis=1)
            for piece in (rep_out, np.stack([gap] * n_capture_channels, axis=1)):
                room = expected_frames - frames_done
                take = min(len(piece), room)
                if take <= 0:
                    break
                chunks.append(piece[:take])
                frames_done += take
                if on_progress is not None:
                    on_progress(frames_done, expected_frames)
                if (sc.truncate_at_frame is not None
                        and frames_done >= sc.truncate_at_frame):
                    break
                if (sc.device_loss_at_frame is not None
                        and frames_done >= sc.device_loss_at_frame):
                    outcome = 'device_lost'
                    events.append(CaptureEvent(
                        kind='device_event', at_frame=frames_done,
                        detail='simulated device loss',
                    ))
                    break
            if outcome == 'device_lost':
                break

        samples = (np.concatenate(chunks, axis=0) if chunks else
                   np.zeros((0, n_capture_channels), dtype=np.float64))
        truncated = (
            outcome == 'completed'
            and len(samples) < expected_frames
            and sc.truncate_at_frame is not None
        )
        captured_channels = (routing.capture_channel,) + (
            (routing.loopback_input_channel,)
            if routing.loopback_input_channel is not None else ()
        )
        return CaptureResult(
            outcome=outcome,
            actual_sample_rate_hz=rate,
            actual_sample_format=self._config.sample_format,
            captured_channels=captured_channels,
            samples=samples,
            expected_frames=expected_frames,
            recorded_frames=len(samples),
            truncated=truncated,
            xrun_count=xruns,
            clipped_samples=clipped_total,
            events=tuple(events),
        )


class FakeAudioBackend(AudioIOBackend):
    """Deterministic in-process backend.

    Simulated completion is test evidence only — it exercises the engine
    and the evidence pipeline without claiming a real acoustic capture.
    """

    backend_id = _FAKE_BACKEND_ID
    backend_version = _FAKE_BACKEND_VERSION

    def __init__(self, scenario: FakeBackendScenario | None = None):
        self.scenario = scenario or default_fake_scenario()
        self.opened_configs: list[AudioStreamConfig] = []

    def available(self) -> bool:
        return True

    def enumerate_devices(self) -> tuple[AudioDeviceInfo, ...]:
        return self.scenario.devices

    def open_stream(self, config: AudioStreamConfig) -> AcquisitionStream:
        devices = {d.device_id: d for d in self.scenario.devices}
        routing = config.routing
        for dev_id, direction in (
            (routing.playback_device_id, 'playback'),
            (routing.capture_device_id, 'capture'),
        ):
            dev = devices.get(dev_id)
            if dev is None:
                raise DeviceNotFoundError(f'unknown device: {dev_id}')
            if direction == 'playback' and dev.direction not in ('playback', 'duplex'):
                raise UnsupportedConfigurationError(
                    f'{dev_id} has no playback direction')
            if direction == 'capture' and dev.direction not in ('capture', 'duplex'):
                raise UnsupportedConfigurationError(
                    f'{dev_id} has no capture direction')
        play = devices[routing.playback_device_id]
        cap = devices[routing.capture_device_id]
        if routing.playback_channel >= max(1, play.max_output_channels):
            raise UnsupportedConfigurationError(
                'playback_channel out of range')
        if routing.capture_channel >= max(1, cap.max_input_channels):
            raise UnsupportedConfigurationError(
                'capture_channel out of range')
        if (routing.loopback_input_channel is not None
                and routing.loopback_input_channel
                >= max(1, cap.max_input_channels)):
            raise UnsupportedConfigurationError(
                'loopback_input_channel out of range')
        if config.sample_rate_hz not in play.supported_sample_rates:
            raise UnsupportedConfigurationError(
                f'sample_rate_hz {config.sample_rate_hz} unsupported')
        if config.sample_format not in play.supported_formats:
            raise UnsupportedConfigurationError(
                f'sample_format {config.sample_format} unsupported')
        self.opened_configs.append(config)
        return _FakeStream(self, config)


# ---------------------------------------------------------------------------
# Timing reference resolution
# ---------------------------------------------------------------------------

TimingQuality = Literal['qualified', 'limited', 'unqualified']
TimingResolutionMethod = Literal[
    'loopback_cross_correlation',
    'stimulus_onset_detection',
    'unsynchronized_declared',
    'unqualified',
]


@dataclass(frozen=True)
class TimingResolution:
    """Explicit timing evidence — never silently aligned into validity."""

    method: TimingResolutionMethod
    quality: TimingQuality
    onset_sample_index: int | None        # block start inside the recording
    playback_to_capture_latency_s: float | None
    drift_ppm: float | None
    reference_channel: int | None
    per_repetition_onsets: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()


def _xcorr_offset(reference: np.ndarray, target: np.ndarray) -> tuple[int, float]:
    """Index where ``reference`` best aligns inside ``target``, and a
    peak-vs-correlation-floor ratio. A sweep's autocorrelation has strong
    sidelobes, so 'second peak' is a poor prominence metric — instead the
    peak is compared to the RMS of the correlation floor outside a guard
    window around it. Deterministic via FFT."""

    n = len(target) + len(reference) - 1
    nfft = 1 << (n - 1).bit_length()
    r = np.fft.rfft(target, nfft)
    s = np.fft.rfft(reference, nfft)
    cc = np.fft.irfft(r * np.conj(s), nfft)
    # cc[k] = correlation at lag k >= 0 (reference delayed by k).
    lags = np.abs(cc[: len(target)])
    if len(lags) == 0:
        return 0, 0.0
    peak_lag = int(np.argmax(lags))
    peak = float(lags[peak_lag])
    if peak <= 0:
        return peak_lag, 0.0
    guard = max(8, len(reference) // 16)
    floor = np.concatenate([
        lags[: max(0, peak_lag - guard)],
        lags[peak_lag + guard:],
    ])
    floor_rms = float(np.sqrt(np.mean(floor ** 2))) if len(floor) else 0.0
    if floor_rms <= 0:
        return peak_lag, float('inf')
    return peak_lag, peak / floor_rms


class TimingReferenceResolver:
    """Derives timing evidence for a capture.

    Loopback reference -> qualified. Declared synchronous topology or a
    detected stimulus onset -> limited. Anything else -> unqualified.
    """

    min_corr_prominence: float = 10.0

    def resolve(
        self,
        capture: CaptureResult,
        stimulus: GeneratedStimulus,
        routing: ChannelRouting,
        declared_synchronized: bool = False,
    ) -> TimingResolution:
        block = np.asarray(stimulus.samples, dtype=np.float64)
        rate = capture.actual_sample_rate_hz
        reasons: list[str] = []
        reps = _rep_onsets(capture, stimulus, routing)
        # Lock against the FIRST repetition's window — a later rep can
        # correlate slightly better and must not steal the onset.
        rep_span = (stimulus.block_frames
                    + int(round(stimulus.spec.repetition_gap_s * rate)))

        if (routing.loopback_input_channel is not None
                and len(capture.captured_channels) >= 2
                and capture.recorded_frames > 0):
            loop = capture.channel_samples(len(capture.captured_channels) - 1)
            offset, prominence = _xcorr_offset(block, loop[:rep_span])
            if prominence >= self.min_corr_prominence and offset >= 0:
                drift = _drift_ppm(reps, stimulus, rate)
                return TimingResolution(
                    method='loopback_cross_correlation',
                    quality='qualified',
                    onset_sample_index=offset,
                    playback_to_capture_latency_s=offset / rate,
                    drift_ppm=drift,
                    reference_channel=routing.loopback_input_channel,
                    per_repetition_onsets=reps,
                    reasons=('wired loopback reference locked',),
                )
            reasons.append('loopback reference present but correlation weak')

        onset = _detect_stimulus_onset(capture, stimulus)
        if onset is not None:
            method: TimingResolutionMethod = 'stimulus_onset_detection'
            if declared_synchronized:
                reasons.append('declared synchronous topology + detected onset')
            else:
                reasons.append('post-hoc onset detection only — unqualified path')
            drift = _drift_ppm(reps, stimulus, rate)
            return TimingResolution(
                method=method,
                quality='limited',
                onset_sample_index=onset,
                playback_to_capture_latency_s=onset / rate,
                drift_ppm=drift,
                reference_channel=None,
                per_repetition_onsets=reps,
                reasons=tuple(reasons),
            )

        if declared_synchronized:
            return TimingResolution(
                method='unsynchronized_declared',
                quality='unqualified',
                onset_sample_index=None,
                playback_to_capture_latency_s=None,
                drift_ppm=None,
                reference_channel=None,
                reasons=(
                    'topology declared synchronous but no onset could be '
                    'detected — timing stays unqualified',
                ),
            )
        return TimingResolution(
            method='unqualified',
            quality='unqualified',
            onset_sample_index=None,
            playback_to_capture_latency_s=None,
            drift_ppm=None,
            reference_channel=None,
            reasons=tuple(reasons) or (
                'no loopback reference and no declared synchronous topology',
            ),
        )


def _detect_stimulus_onset(
    capture: CaptureResult, stimulus: GeneratedStimulus,
) -> int | None:
    """Find the stimulus block's start inside the measurement channel via
    cross-correlation; conservative — returns None when the peak is not
    prominent enough to trust."""

    if capture.recorded_frames == 0:
        return None
    meas = capture.channel_samples(0)
    block = np.asarray(stimulus.samples, dtype=np.float64)
    gap = int(round(stimulus.spec.repetition_gap_s
                    * capture.actual_sample_rate_hz))
    rep_span = len(block) + gap
    offset, prominence = _xcorr_offset(block, meas[:rep_span])
    if prominence < 8.0 or offset < 0:
        return None
    return int(offset)


def _rep_onsets(
    capture: CaptureResult,
    stimulus: GeneratedStimulus,
    routing: ChannelRouting,
) -> tuple[int, ...]:
    """Per-repetition onset estimates (loopback first, else measurement)."""

    if capture.recorded_frames == 0:
        return ()
    ch = (len(capture.captured_channels) - 1
          if routing.loopback_input_channel is not None
          and len(capture.captured_channels) >= 2 else 0)
    sig = capture.channel_samples(ch)
    block = np.asarray(stimulus.samples, dtype=np.float64)
    gap = int(round(stimulus.spec.repetition_gap_s
                    * capture.actual_sample_rate_hz))
    rep_len = len(block) + gap
    onsets: list[int] = []
    for rep in range(stimulus.spec.repetitions):
        lo = rep * rep_len
        hi = min(lo + rep_len, len(sig))
        if hi - lo < len(block):
            break
        off, prom = _xcorr_offset(block, sig[lo:hi])
        if prom >= 8.0 and off >= 0:
            onsets.append(lo + off)
    return tuple(onsets)


def _drift_ppm(
    onsets: tuple[int, ...],
    stimulus: GeneratedStimulus,
    rate: int,
) -> float | None:
    """Observed vs nominal inter-repetition spacing, in ppm of one rep."""

    if len(onsets) < 2:
        return None
    nominal = (stimulus.block_frames
               + int(round(stimulus.spec.repetition_gap_s * rate)))
    if nominal <= 0:
        return None
    observed = (onsets[-1] - onsets[0]) / (len(onsets) - 1)
    return (observed - nominal) / nominal * 1e6


# ---------------------------------------------------------------------------
# Impulse response derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedImpulseResponse:
    """A derived IR artifact + identity. Separate evidence from the raw
    recording; a version change never mutates historical artifacts."""

    status: Literal['derived', 'timing_unqualified', 'failed']
    derivation_version: str
    sample_rate_hz: int
    ir: tuple[float, ...] | None
    ir_sha256: str | None
    origin_sample_index: int | None     # where the IR window starts in the deconv
    origin_convention: str              # 'detected_onset' | 'loopback_reference' | 'none'
    window_seconds: float
    frequency_response: tuple[tuple[float, float], ...] | None  # (hz, dB)
    regularization: str
    notes: tuple[str, ...] = ()


class ImpulseResponseDeriver:
    """Versioned inverse-sweep deconvolution + latency alignment."""

    def __init__(
        self,
        derivation_version: str = IR_DERIVATION_VERSION,
        regularization: float = 1e-4,
        window_seconds: float = 0.5,
    ):
        self.derivation_version = derivation_version
        self.regularization = regularization
        self.window_seconds = window_seconds

    def derive(
        self,
        stimulus: GeneratedStimulus,
        capture: CaptureResult,
        timing: TimingResolution,
        channel_position: int = 0,
    ) -> DerivedImpulseResponse:
        if capture.recorded_frames == 0:
            return DerivedImpulseResponse(
                status='failed', derivation_version=self.derivation_version,
                sample_rate_hz=capture.actual_sample_rate_hz,
                ir=None, ir_sha256=None, origin_sample_index=None,
                origin_convention='none', window_seconds=self.window_seconds,
                frequency_response=None,
                regularization=f'relative_{self.regularization:g}',
                notes=('empty capture',),
            )
        if timing.onset_sample_index is None:
            return DerivedImpulseResponse(
                status='timing_unqualified',
                derivation_version=self.derivation_version,
                sample_rate_hz=capture.actual_sample_rate_hz,
                ir=None, ir_sha256=None, origin_sample_index=None,
                origin_convention='none', window_seconds=self.window_seconds,
                frequency_response=None,
                regularization=f'relative_{self.regularization:g}',
                notes=timing.reasons or ('no qualified timing evidence',),
            )

        block = np.asarray(stimulus.samples, dtype=np.float64)
        rec = capture.channel_samples(channel_position)
        rate = capture.actual_sample_rate_hz
        n = len(rec) + len(block) - 1
        nfft = 1 << (n - 1).bit_length()
        r = np.fft.rfft(rec, nfft)
        s = np.fft.rfft(block, nfft)
        mag2 = np.abs(s) ** 2
        eps = self.regularization * float(mag2.max()) if mag2.max() > 0 else 1.0
        y = np.fft.irfft(r * np.conj(s) / (mag2 + eps), nfft)
        # conj(S) is matched filtering: the deconvolved impulse response h[k]
        # lands at the detected block-onset index, i.e. y[onset + k] = h[k].
        # With a loopback reference the onset is the electrical reference
        # time, so the acoustic flight time stays visible as the leading
        # offset; with post-hoc onset detection it is the acoustic arrival.
        window = int(round(self.window_seconds * rate))
        guard = min(64, timing.onset_sample_index)
        start = timing.onset_sample_index - guard
        end = min(len(y), start + window)
        if end <= start:
            return DerivedImpulseResponse(
                status='failed', derivation_version=self.derivation_version,
                sample_rate_hz=rate, ir=None, ir_sha256=None,
                origin_sample_index=None, origin_convention='none',
                window_seconds=self.window_seconds, frequency_response=None,
                regularization=f'relative_{self.regularization:g}',
                notes=('ir window empty',),
            )
        ir = np.asarray(y[start:end], dtype=np.float64)
        # Deterministic normalization by the deconvolution peak.
        peak = float(np.max(np.abs(ir))) if len(ir) else 0.0
        if peak > 0:
            ir = ir / peak
        origin_convention = (
            'loopback_reference'
            if timing.method == 'loopback_cross_correlation'
            else 'detected_onset'
        )
        # Optional frequency response over the IR window.
        fr: tuple[tuple[float, float], ...] | None = None
        if len(ir) >= 8:
            spec = np.fft.rfft(ir, 1 << (len(ir) - 1).bit_length())
            freqs = np.fft.rfftfreq(1 << (len(ir) - 1).bit_length(), 1.0 / rate)
            mags = 20.0 * np.log10(np.maximum(np.abs(spec), 1e-15))
            fr = tuple(
                (round(float(f), 6), round(float(m), 6))
                for f, m in zip(freqs, mags)
            )
        return DerivedImpulseResponse(
            status='derived',
            derivation_version=self.derivation_version,
            sample_rate_hz=rate,
            ir=tuple(float(x) for x in ir),
            ir_sha256=_samples_sha256(ir),
            origin_sample_index=start,
            origin_convention=origin_convention,
            window_seconds=self.window_seconds,
            frequency_response=fr,
            regularization=f'relative_{self.regularization:g}',
            notes=(),
        )


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

AcquisitionQualityVerdict = Literal['valid', 'limited', 'invalid', 'unevaluated']
AcquisitionQualityReason = Literal[
    'clipping_detected',
    'insufficient_snr',
    'capture_truncated',
    'xrun_detected',
    'device_lost',
    'calibration_missing',
    'calibration_invalid',
    'synchronization_unusable',
    'timing_limited',
    'excessive_noise',
    'inconsistent_repetitions',
    'cancelled',
    'backend_unavailable',
    'open_failed',
]
CalibrationBindingState = Literal['bound', 'missing', 'invalid', 'unknown']


@dataclass(frozen=True)
class QualityGateThresholds:
    """Configurable limits — every one fails closed when exceeded."""

    min_snr_db: float = 20.0
    max_noise_floor_dbfs: float = -40.0
    max_clipped_samples: int = 0
    max_xruns: int = 0
    min_repetition_consistency: float = 0.9   # corr between repeated captures


@dataclass(frozen=True)
class AcquisitionQualityReport:
    verdict: AcquisitionQualityVerdict
    reasons: tuple[AcquisitionQualityReason, ...]
    warnings: tuple[str, ...]
    measured: Mapping[str, float | int | None]


def _noise_floor_dbfs(capture: CaptureResult, onset: int | None) -> float | None:
    """Estimate the noise floor from the pre-onset region (pre-roll silence)."""

    if capture.recorded_frames == 0:
        return None
    meas = capture.channel_samples(0)
    end = onset if onset is not None and onset > 8 else min(64, len(meas))
    floor = meas[:max(8, end)]
    rms = float(np.sqrt(np.mean(floor ** 2))) if len(floor) else 0.0
    if rms <= 0:
        return -300.0
    return 20.0 * math.log10(rms)


def _snr_db(capture: CaptureResult, stimulus: GeneratedStimulus,
            onset: int | None) -> float | None:
    if capture.recorded_frames == 0:
        return None
    meas = capture.channel_samples(0)
    lo = onset if onset is not None else 0
    hi = min(len(meas), lo + stimulus.sweep_samples)
    if hi <= lo:
        return None
    sig_rms = float(np.sqrt(np.mean(meas[lo:hi] ** 2)))
    floor_dbfs = _noise_floor_dbfs(capture, onset)
    if floor_dbfs is None:
        return None
    noise_rms = math.pow(10.0, floor_dbfs / 20.0)
    if noise_rms <= 0:
        return 300.0
    return 20.0 * math.log10(max(sig_rms, 1e-15) / noise_rms)


def _repetition_consistency(
    capture: CaptureResult, stimulus: GeneratedStimulus,
    onsets: tuple[int, ...],
) -> float | None:
    """Correlation between the first two detected repetitions."""

    if len(onsets) < 2:
        return None
    meas = capture.channel_samples(0)
    n = stimulus.sweep_samples
    a = meas[onsets[0]: onsets[0] + n]
    b = meas[onsets[1]: onsets[1] + n]
    if len(a) < 8 or len(b) < 8 or len(a) != len(b):
        return None
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0 or nb <= 0:
        return None
    return float(np.dot(a, b) / (na * nb))


def evaluate_acquisition_quality(
    capture: CaptureResult | None,
    stimulus: GeneratedStimulus | None,
    timing: TimingResolution | None,
    calibration_state: CalibrationBindingState,
    requires_absolute_level: bool = False,
    thresholds: QualityGateThresholds = QualityGateThresholds(),
) -> AcquisitionQualityReport:
    """The quality gate. A completed recording is not automatically valid."""

    reasons: list[AcquisitionQualityReason] = []
    warnings: list[str] = []
    measured: dict[str, float | int | None] = {}

    if capture is None:
        return AcquisitionQualityReport(
            verdict='invalid', reasons=('open_failed',), warnings=(),
            measured={},
        )
    if capture.outcome == 'cancelled':
        reasons.append('cancelled')
    if capture.outcome == 'device_lost':
        reasons.append('device_lost')
    if capture.truncated or capture.recorded_frames < capture.expected_frames:
        reasons.append('capture_truncated')
    if capture.clipped_samples > thresholds.max_clipped_samples:
        reasons.append('clipping_detected')
    if capture.xrun_count > thresholds.max_xruns:
        reasons.append('xrun_detected')

    onset = timing.onset_sample_index if timing else None
    measured['clipped_samples'] = capture.clipped_samples
    measured['xrun_count'] = capture.xrun_count
    measured['recorded_frames'] = capture.recorded_frames
    measured['expected_frames'] = capture.expected_frames

    noise_dbfs = _noise_floor_dbfs(capture, onset)
    measured['noise_floor_dbfs'] = noise_dbfs
    if noise_dbfs is not None and noise_dbfs > thresholds.max_noise_floor_dbfs:
        reasons.append('excessive_noise')

    snr = (_snr_db(capture, stimulus, onset)
           if stimulus is not None else None)
    measured['snr_db'] = snr
    if snr is not None and snr < thresholds.min_snr_db:
        reasons.append('insufficient_snr')

    consistency = (_repetition_consistency(capture, stimulus,
                                           timing.per_repetition_onsets)
                   if stimulus is not None and timing is not None else None)
    measured['repetition_consistency'] = consistency
    if (consistency is not None
            and consistency < thresholds.min_repetition_consistency):
        reasons.append('inconsistent_repetitions')

    if timing is None or timing.quality == 'unqualified':
        reasons.append('synchronization_unusable')
    elif timing.quality == 'limited':
        warnings.append('timing evidence is limited (no wired reference)')

    if requires_absolute_level:
        if calibration_state == 'missing':
            reasons.append('calibration_missing')
        elif calibration_state == 'invalid':
            reasons.append('calibration_invalid')
        elif calibration_state == 'unknown':
            reasons.append('calibration_missing')
    elif calibration_state in ('missing', 'unknown'):
        warnings.append('no calibration bound — absolute SPL stays UNKNOWN')
    elif calibration_state == 'invalid':
        reasons.append('calibration_invalid')

    # Distinct deterministic order.
    order = (
        'clipping_detected', 'insufficient_snr', 'capture_truncated',
        'xrun_detected', 'device_lost', 'synchronization_unusable',
        'calibration_invalid', 'calibration_missing', 'excessive_noise',
        'inconsistent_repetitions', 'cancelled', 'open_failed',
        'backend_unavailable', 'timing_limited',
    )
    ordered = tuple(r for r in order if r in reasons)
    if 'cancelled' in ordered or 'device_lost' in ordered or 'open_failed' in ordered:
        verdict: AcquisitionQualityVerdict = 'invalid'
    elif not ordered:
        verdict = 'valid' if not warnings else 'limited'
    elif timing is not None and timing.quality != 'unqualified' and ordered == (
        'timing_limited',
    ):
        verdict = 'limited'
    else:
        verdict = 'invalid'
    return AcquisitionQualityReport(
        verdict=verdict, reasons=ordered, warnings=tuple(warnings),
        measured=measured,
    )


# ---------------------------------------------------------------------------
# Acquisition engine state machine
# ---------------------------------------------------------------------------

AcquisitionStage = Literal[
    'precheck', 'ready', 'armed', 'playing_recording',
    'processing', 'quality_check', 'completed', 'failed', 'cancelled',
]

_TERMINAL_STAGES = ('completed', 'failed', 'cancelled')


class ArmConfirmation(BaseModel):
    """What the operator saw and confirmed before output is armed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    acknowledged_playback_device_id: str
    acknowledged_playback_channel: int = Field(ge=0)
    acknowledged_capture_device_id: str
    acknowledged_capture_channel: int = Field(ge=0)
    acknowledged_level_dbfs: float


class LevelSafetyPolicy(BaseModel):
    """Configurable level limits — exceeding them fails closed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    max_output_level_dbfs: float = -6.0
    require_loopback_reference: bool = False


@dataclass(frozen=True)
class PrecheckReport:
    ok: bool
    blocked_reasons: tuple[str, ...]
    playback_device: AudioDeviceInfo | None
    capture_device: AudioDeviceInfo | None
    calibration_state: CalibrationBindingState


@dataclass(frozen=True)
class AcquisitionRequest:
    """Everything one measurement run needs."""

    stimulus: SweepStimulusSpec
    routing: ChannelRouting
    level_policy: LevelSafetyPolicy = LevelSafetyPolicy()
    requires_absolute_level: bool = False
    calibration_state: CalibrationBindingState = 'unknown'
    declared_synchronized: bool = False
    quality_thresholds: QualityGateThresholds = QualityGateThresholds()


@dataclass(frozen=True)
class StageTransition:
    stage: AcquisitionStage
    reason: str
    at_utc: str


@dataclass
class AcquisitionResult:
    """The retained result of one run — feeds the sealed evidence record."""

    request: AcquisitionRequest | None = None
    stimulus: GeneratedStimulus | None = None
    stream_config: AudioStreamConfig | None = None
    capture: CaptureResult | None = None
    timing: TimingResolution | None = None
    impulse_response: DerivedImpulseResponse | None = None
    quality: AcquisitionQualityReport | None = None
    precheck: PrecheckReport | None = None


class MeasurementAcquisitionEngine:
    """PRECHECK -> READY -> ARMED -> PLAYING/RECORDING -> PROCESSING ->
    QUALITY_CHECK -> COMPLETED/FAILED/CANCELLED.

    Every transition records an explicit reason. Configuration changes
    after arm invalidate the armed state back to READY. Cancel is
    first-class from any non-terminal stage.
    """

    def __init__(
        self,
        backend: AudioIOBackend,
        stimulus_generator: SweepStimulusGenerator | None = None,
        deriver: ImpulseResponseDeriver | None = None,
        resolver: TimingReferenceResolver | None = None,
        clock: Callable[[], str] = _utc_now,
        engine_version: str = SWEEP_ACQUISITION_ENGINE_VERSION,
    ):
        self.backend = backend
        self.stimulus_generator = stimulus_generator or SweepStimulusGenerator()
        self.deriver = deriver or ImpulseResponseDeriver()
        self.resolver = resolver or TimingReferenceResolver()
        self._clock = clock
        self.engine_version = engine_version
        self._stage: AcquisitionStage = 'precheck'
        self._history: list[StageTransition] = [
            StageTransition('precheck', 'engine constructed', self._clock())]
        self._result = AcquisitionResult()
        self._blocked: tuple[str, ...] = ('not configured',)
        self._cancel = False
        self._armed_config_sha: str | None = None
        self._progress: tuple[int, int] = (0, 0)
        self.run_id = f'run-{uuid.uuid4().hex[:16]}'

    # -- introspection ----------------------------------------------------

    @property
    def stage(self) -> AcquisitionStage:
        return self._stage

    @property
    def transitions(self) -> tuple[StageTransition, ...]:
        return tuple(self._history)

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return self._blocked

    @property
    def progress(self) -> tuple[int, int]:
        return self._progress

    @property
    def result(self) -> AcquisitionResult:
        return self._result

    def next_permitted_action(self) -> str:
        return {
            'precheck': 'configure() — resolve devices and routing',
            'ready': 'arm(confirmation) — confirm device/channel/level',
            'armed': 'start() — begin playback+recording, or cancel()',
            'playing_recording': 'wait or cancel()',
            'processing': 'wait',
            'quality_check': 'wait',
            'completed': 'inspect evidence; configure() to start a new run',
            'failed': 'configure() to retry after fixing the cause',
            'cancelled': 'configure() to start a new run',
        }[self._stage]

    def _transition(self, stage: AcquisitionStage, reason: str) -> None:
        self._stage = stage
        self._history.append(StageTransition(stage, reason, self._clock()))
        # #883: the session journal mirrors the stage so a mid-run crash
        # reads ACQUISITION_INCOMPLETE instead of a completed measurement.
        try:
            from .session_recovery import (
                declare_acquisition_stage,
                declare_operation_finished,
                declare_operation_started,
            )

            declare_acquisition_stage(self.run_id, stage)
            if stage in _TERMINAL_STAGES:
                declare_operation_finished(self.run_id)
            elif stage == 'ready':
                # First non-precheck stage of a configured run — the run
                # is now a real in-flight operation.
                declare_operation_started(
                    'sweep_acquisition', self.run_id
                )
        except Exception:
            pass

    # -- PRECHECK ----------------------------------------------------------

    def configure(self, request: AcquisitionRequest) -> PrecheckReport:
        if self._stage == 'armed':
            # Reconfiguring after arm invalidates the armed state.
            self._armed_config_sha = None
            self._transition('ready', 'reconfigured — armed state invalidated')
        if self._stage in _TERMINAL_STAGES:
            self._transition('precheck', 'new run configured after terminal state')
            self._result = AcquisitionResult()
            self._progress = (0, 0)
            self._cancel = False
            self.run_id = f'run-{uuid.uuid4().hex[:16]}'

        blocked: list[str] = []
        geometry = request.stimulus.validate_geometry()
        blocked.extend(geometry)

        devices = self.backend.enumerate_devices()
        by_id = {d.device_id: d for d in devices}
        play = by_id.get(request.routing.playback_device_id)
        cap = by_id.get(request.routing.capture_device_id)
        if not devices:
            blocked.append('no audio devices enumerated')
        if play is None:
            blocked.append(
                f'playback device {request.routing.playback_device_id} '
                'not enumerated')
        if cap is None:
            blocked.append(
                f'capture device {request.routing.capture_device_id} '
                'not enumerated')
        if play is not None:
            if request.routing.playback_channel >= play.max_output_channels:
                blocked.append('playback channel out of range')
            if request.stimulus.sample_rate_hz not in play.supported_sample_rates:
                blocked.append('requested sample rate unsupported by playback device')
        if cap is not None:
            if request.routing.capture_channel >= cap.max_input_channels:
                blocked.append('capture channel out of range')
            if (request.routing.loopback_input_channel is not None
                    and request.routing.loopback_input_channel
                    >= cap.max_input_channels):
                blocked.append('loopback channel out of range')

        self._result.request = request
        report = PrecheckReport(
            ok=not blocked, blocked_reasons=tuple(blocked),
            playback_device=play, capture_device=cap,
            calibration_state=request.calibration_state,
        )
        self._result.precheck = report
        self._blocked = report.blocked_reasons
        if report.ok:
            self._transition('ready', 'precheck passed — devices and routing bound')
        else:
            self._transition('precheck', 'precheck failed: ' + '; '.join(blocked))
        return report

    # -- ARM (level safety gate) --------------------------------------------

    def arm_blockers(self, request: AcquisitionRequest) -> tuple[str, ...]:
        reasons: list[str] = []
        if self._stage != 'ready':
            reasons.append(f'engine is {self._stage}, not ready')
        if request.stimulus.level_dbfs > request.level_policy.max_output_level_dbfs:
            reasons.append(
                f'requested level {request.stimulus.level_dbfs} dBFS exceeds '
                f'policy limit {request.level_policy.max_output_level_dbfs} dBFS')
        if (request.level_policy.require_loopback_reference
                and request.routing.loopback_input_channel is None):
            reasons.append('loopback reference required by policy but not routed')
        return tuple(reasons)

    def arm(self, confirmation: ArmConfirmation) -> None:
        request = self._result.request
        if request is None:
            raise AcquisitionStateError('configure() first')
        blockers = list(self.arm_blockers(request))
        r = request.routing
        mismatches = []
        if confirmation.acknowledged_playback_device_id != r.playback_device_id:
            mismatches.append('playback device')
        if confirmation.acknowledged_playback_channel != r.playback_channel:
            mismatches.append('playback channel')
        if confirmation.acknowledged_capture_device_id != r.capture_device_id:
            mismatches.append('capture device')
        if confirmation.acknowledged_capture_channel != r.capture_channel:
            mismatches.append('capture channel')
        if confirmation.acknowledged_level_dbfs != request.stimulus.level_dbfs:
            mismatches.append('level')
        if mismatches:
            blockers.append(
                'arm confirmation does not match armed config: '
                + ', '.join(mismatches))
        if blockers:
            self._blocked = tuple(blockers)
            raise ArmBlockedError(tuple(blockers))
        # Armed snapshot — any later configure() invalidates this.
        self._armed_config_sha = canonical_sha256({
            'request': {
                'level_dbfs': request.stimulus.level_dbfs,
                'sample_rate_hz': request.stimulus.sample_rate_hz,
                'playback_device_id': r.playback_device_id,
                'playback_channel': r.playback_channel,
                'capture_device_id': r.capture_device_id,
                'capture_channel': r.capture_channel,
                'loopback_input_channel': r.loopback_input_channel,
            },
        })
        self._blocked = ()
        self._transition(
            'armed',
            f'operator confirmed {r.playback_device_id}:'
            f'{r.playback_channel} @ {request.stimulus.level_dbfs} dBFS')

    def notify_configuration_changed(self) -> None:
        """External signal (device hot-swap, routing edit) — armed state is
        stale and must be re-confirmed."""

        if self._stage == 'armed':
            self._armed_config_sha = None
            self._transition('ready', 'configuration changed — armed invalidated')
            self._blocked = ('armed state invalidated by configuration change',)

    # -- START --------------------------------------------------------------

    def start(self) -> AcquisitionResult:
        if self._stage != 'armed' or self._armed_config_sha is None:
            raise AcquisitionStateError(
                f'cannot start from stage {self._stage}')
        request = self._result.request
        assert request is not None
        stimulus = self.stimulus_generator.generate(request.stimulus)
        self._result.stimulus = stimulus
        config = AudioStreamConfig(
            sample_rate_hz=request.stimulus.sample_rate_hz,
            sample_format='float64',
            routing=request.routing,
        )
        self._result.stream_config = config
        self._transition('playing_recording', 'stream opening')

        try:
            stream = self.backend.open_stream(config)
        except BackendUnavailableError as exc:
            self._transition('failed', f'backend unavailable: {exc}')
            self._result.quality = AcquisitionQualityReport(
                verdict='invalid', reasons=('backend_unavailable',),
                warnings=(), measured={})
            return self._result
        except (DeviceNotFoundError, UnsupportedConfigurationError) as exc:
            self._transition('failed', f'open failed: {exc}')
            self._result.quality = AcquisitionQualityReport(
                verdict='invalid', reasons=('open_failed',),
                warnings=(), measured={})
            return self._result

        capture = stream.run_acquisition(
            np.asarray(stimulus.samples, dtype=np.float64),
            repetitions=request.stimulus.repetitions,
            gap_samples=int(round(request.stimulus.repetition_gap_s
                                  * request.stimulus.sample_rate_hz)),
            should_cancel=lambda: self._cancel,
            on_progress=self._on_progress,
        )
        self._result.capture = capture
        if capture.outcome == 'cancelled' or self._cancel:
            self._transition('cancelled', 'operator cancelled during capture')
        elif capture.outcome == 'device_lost':
            self._transition('failed', 'device lost mid-capture')
        else:
            self._transition('processing', 'capture finished — deriving evidence')

        if self._stage in ('cancelled', 'failed'):
            self._result.quality = evaluate_acquisition_quality(
                capture, stimulus, None, request.calibration_state,
                requires_absolute_level=request.requires_absolute_level,
                thresholds=request.quality_thresholds,
            )
            return self._result

        # PROCESSING: timing + IR.
        timing = self.resolver.resolve(
            capture, stimulus, request.routing,
            declared_synchronized=request.declared_synchronized,
        )
        self._result.timing = timing
        ir = self.deriver.derive(stimulus, capture, timing)
        self._result.impulse_response = ir
        self._transition(
            'quality_check',
            f'timing={timing.quality} ir={ir.status} — evaluating quality')
        quality = evaluate_acquisition_quality(
            capture, stimulus, timing, request.calibration_state,
            requires_absolute_level=request.requires_absolute_level,
            thresholds=request.quality_thresholds,
        )
        self._result.quality = quality
        self._transition(
            'completed',
            f'acquisition finished — quality verdict={quality.verdict} '
            f'reasons={list(quality.reasons)}')
        self._blocked = ()
        return self._result

    def _on_progress(self, done: int, total: int) -> None:
        self._progress = (done, total)

    # -- CANCEL ---------------------------------------------------------------

    def cancel(self) -> None:
        if self._stage in _TERMINAL_STAGES:
            return
        self._cancel = True
        if self._stage in ('precheck', 'ready', 'armed'):
            self._transition('cancelled', 'operator cancelled before capture')


__all__ = [
    'AcquisitionQualityReport', 'AcquisitionQualityReason',
    'AcquisitionQualityVerdict', 'AcquisitionRequest', 'AcquisitionResult',
    'AcquisitionStage', 'AcquisitionStateError', 'AcquisitionStream',
    'ArmBlockedError', 'ArmConfirmation', 'AudioDeviceInfo', 'AudioIOBackend',
    'AudioStreamConfig', 'BackendUnavailableError', 'CalibrationBindingState',
    'CaptureEvent', 'CaptureOutcome', 'CaptureResult', 'ChannelRouting',
    'DerivedImpulseResponse', 'DeviceNotFoundError', 'FakeAudioBackend',
    'FakeBackendScenario', 'GeneratedStimulus', 'ImpulseResponseDeriver',
    'IR_DERIVATION_VERSION', 'LevelSafetyPolicy', 'MeasurementAcquisitionEngine',
    'PrecheckReport', 'QualityGateThresholds', 'StageTransition',
    'SweepAcquisitionError', 'SweepStimulusGenerator', 'SweepStimulusSpec',
    'SWEEP_ACQUISITION_ENGINE_VERSION', 'SWEEP_ACQUISITION_SCHEMA_VERSION',
    'SWEEP_GENERATOR_VERSION', 'TimingQuality', 'TimingReferenceResolver',
    'TimingResolution', 'TimingResolutionMethod',
    'UnsupportedConfigurationError', 'WasapiAudioBackend',
    'default_fake_devices', 'default_fake_scenario',
    'evaluate_acquisition_quality',
]
