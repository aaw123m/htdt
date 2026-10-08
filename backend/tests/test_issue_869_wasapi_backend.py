"""#869 WASAPI audio backend tests (REV70).

The real ``WasapiAudioBackend`` drives shared-mode render+capture through
a narrow injected driver seam (``WasapiDriverBase`` in
``htdt.cad_wasapi_io``). These tests exercise every non-hardware path —
endpoint mapping, channel-mask labels, availability staging, config
negotiation, the pump loop's outcome handling — against a scripted fake
driver, plus a guarded smoke test against real endpoints when the box
exposes them. No test here claims acoustic evidence; a scripted capture
is still simulated input, and ``backend_is_simulated`` stays honest
because the fake feeds the REAL backend code path, not a simulated one.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from htdt.cad_sweep_acquisition import (
    AudioStreamConfig,
    BackendUnavailableError,
    ChannelRouting,
    DeviceNotFoundError,
    MeasurementAcquisitionEngine,
    UnsupportedConfigurationError,
    WasapiAudioBackend,
)
from htdt.cad_wasapi_io import (
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
    AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR,
    WasapiCaptureClientBase,
    WasapiDriverBase,
    WasapiDriverError,
    WasapiBusyError,
    WasapiEndpointGoneError,
    WasapiEndpointInfo,
    WasapiRenderClientBase,
    WasapiUnsupportedFormatError,
    channel_mask_labels,
    stereo_pair_channels,
    _WasapiStream,
)

from test_issue_869_native_sweep import (
    _confirmation,
    _request,
)


RENDER_EP = 'wasapi-render-0'
CAPTURE_EP = 'wasapi-capture-0'


def _ep(**kw) -> WasapiEndpointInfo:
    payload = dict(
        endpoint_id=RENDER_EP,
        friendly_name='Test Render Endpoint',
        data_flow='render',
        is_default=True,
        mix_sample_rate_hz=48000,
        mix_channels=2,
        mix_channel_mask=0x3,
        mix_format_tag=3,
        supports_float32=True,
    )
    payload.update(kw)
    return WasapiEndpointInfo(**payload)


def _routing(**kw) -> ChannelRouting:
    payload = dict(
        playback_device_id=RENDER_EP,
        playback_channel=0,
        capture_device_id=CAPTURE_EP,
        capture_channel=0,
        loopback_input_channel=1,
    )
    payload.update(kw)
    return ChannelRouting(**payload)


def _config(**kw) -> AudioStreamConfig:
    payload = dict(sample_rate_hz=48000, sample_format='float64',
                   routing=_routing())
    payload.update(kw)
    return AudioStreamConfig(**payload)


# ---------------------------------------------------------------------------
# Scripted fake driver — same seam the real ctypes driver implements.
# ---------------------------------------------------------------------------


class _FakeRenderClient(WasapiRenderClientBase):
    """A render endpoint with a fixed buffer that 'plays' drained space."""

    def __init__(self, channels: int = 2, rate: int = 48000,
                 buffer_frames: int = 256, drain_per_call: int = 64):
        self.channels = channels
        self.sample_rate_hz = rate
        self.buffer_frames = buffer_frames
        self._drain_per_call = drain_per_call
        self._written = 0
        self._played = 0
        self.blocks: list[np.ndarray] = []
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def free_space_frames(self) -> int:
        self._played = min(self._written,
                           self._played + self._drain_per_call)
        return self.buffer_frames - (self._written - self._played)

    def write_f32(self, frames: np.ndarray) -> int:
        n = min(frames.shape[0],
                self.buffer_frames - (self._written - self._played))
        self.blocks.append(np.asarray(frames[:n], dtype=np.float32))
        self._written += n
        return n

    @property
    def written(self) -> np.ndarray:
        if not self.blocks:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(self.blocks, axis=0)


class _ScriptedCaptureClient(WasapiCaptureClientBase):
    """Capture endpoint replaying a scripted interleaved float32 signal.

    ``flags_at`` maps packet index -> AUDCLNT_BUFFERFLAGS_* bits;
    ``raise_after`` raises the given error once that many packets have
    been served. ``calls_per_packet`` paces availability — a packet is
    served only every Nth read call (the rest report empty), simulating
    real-time capture pacing so multi-iteration pump paths (cancel,
    deadline, mid-run loss) are reachable. ``loopback_of`` mirrors
    another client's written data into ``source_column`` -> this
    client's ``dest_columns`` to simulate a wired loopback/acoustic
    echo for engine-level runs.
    """

    def __init__(
        self,
        signal: np.ndarray | None = None,
        *,
        channels: int = 2,
        rate: int = 48000,
        packet_frames: int = 64,
        calls_per_packet: int = 1,
        flags_at: dict[int, int] | None = None,
        raise_after: int | None = None,
        raise_with: Exception | None = None,
        loopback_of: _FakeRenderClient | None = None,
        loopback_source_channel: int = 0,
        loopback_dest_columns: tuple[int, ...] = (0, 1),
    ):
        self.channels = channels
        self.sample_rate_hz = rate
        self._signal = np.asarray(
            signal if signal is not None else np.zeros((0, channels)),
            dtype=np.float32)
        self._packet_frames = packet_frames
        self._calls_per_packet = max(1, calls_per_packet)
        self._call_index = 0
        self._flags_at = flags_at or {}
        self._raise_after = raise_after
        self._raise_with = raise_with or WasapiEndpointGoneError(
            'endpoint lost')
        self._loopback_of = loopback_of
        self._loopback_src = loopback_source_channel
        self._loopback_dst = loopback_dest_columns
        self._cursor = 0
        self._packet_index = 0
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def _available(self) -> int:
        if self._loopback_of is not None:
            return self._loopback_of._written - self._cursor
        return len(self._signal) - self._cursor

    def read_packet(self) -> tuple[np.ndarray | None, int, int | None]:
        call = self._call_index
        self._call_index += 1
        if (self._raise_after is not None
                and self._packet_index >= self._raise_after):
            raise self._raise_with
        if call % self._calls_per_packet != 0:
            return None, 0, None
        n = min(self._packet_frames, self._available())
        if n <= 0:
            return None, 0, None
        if self._loopback_of is not None:
            src = self._loopback_of.written
            packet = np.zeros((n, self.channels), dtype=np.float32)
            column = src[self._cursor:self._cursor + n,
                         self._loopback_src]
            for dst in self._loopback_dst:
                packet[:, dst] = column
        else:
            packet = self._signal[self._cursor:self._cursor + n]
        flags = self._flags_at.get(self._packet_index, 0)
        self._cursor += n
        self._packet_index += 1
        return packet, flags, self._cursor


class ScriptedWasapiDriver(WasapiDriverBase):
    """The full driver seam, scripted — endpoints, defaults, clients."""

    def __init__(
        self,
        endpoints: tuple[WasapiEndpointInfo, ...] = (),
        *,
        render_client: _FakeRenderClient | None = None,
        capture_client: _ScriptedCaptureClient | None = None,
        render_error: Exception | None = None,
        capture_error: Exception | None = None,
        enumerate_error: Exception | None = None,
        default_ids: dict[str, str | None] | None = None,
        default_changes: dict[str, list[str | None]] | None = None,
    ):
        self._endpoints = endpoints
        self._render_client = render_client
        self._capture_client = capture_client
        self._render_error = render_error
        self._capture_error = capture_error
        self._enumerate_error = enumerate_error
        self._default_ids = dict(
            default_ids or {'render': RENDER_EP, 'capture': CAPTURE_EP})
        self._default_changes = {
            k: list(v) for k, v in (default_changes or {}).items()}
        self.opened: list[tuple[str, str, int, int]] = []

    def enumerate_endpoints(self) -> tuple[WasapiEndpointInfo, ...]:
        if self._enumerate_error is not None:
            raise self._enumerate_error
        return self._endpoints

    def default_endpoint_id(self, data_flow: str) -> str | None:
        changes = self._default_changes.get(data_flow)
        if changes:
            return changes.pop(0)
        return self._default_ids.get(data_flow)

    def open_render(self, endpoint_id: str, sample_rate_hz: int,
                    channels: int) -> WasapiRenderClientBase:
        self.opened.append(('render', endpoint_id, sample_rate_hz,
                            channels))
        if self._render_error is not None:
            raise self._render_error
        client = self._render_client or _FakeRenderClient(
            channels=channels, rate=sample_rate_hz)
        return client

    def open_capture(self, endpoint_id: str, sample_rate_hz: int,
                     channels: int) -> WasapiCaptureClientBase:
        self.opened.append(('capture', endpoint_id, sample_rate_hz,
                            channels))
        if self._capture_error is not None:
            raise self._capture_error
        client = self._capture_client or _ScriptedCaptureClient(
            channels=channels, rate=sample_rate_hz)
        return client


def _duplex_endpoints(**kw) -> tuple[WasapiEndpointInfo, ...]:
    return (
        _ep(**kw),
        _ep(endpoint_id=CAPTURE_EP, friendly_name='Test Capture Endpoint',
            data_flow='capture'),
    )


def _backend(driver: ScriptedWasapiDriver) -> WasapiAudioBackend:
    return WasapiAudioBackend(driver=driver)


def _stream(config: AudioStreamConfig | None = None,
            render: _FakeRenderClient | None = None,
            capture: _ScriptedCaptureClient | None = None,
            driver: ScriptedWasapiDriver | None = None,
            **stream_kw) -> _WasapiStream:
    config = config or _config()
    render = render or _FakeRenderClient(
        channels=2, rate=config.sample_rate_hz)
    capture = capture or _ScriptedCaptureClient(
        channels=2, rate=config.sample_rate_hz)
    driver = driver or ScriptedWasapiDriver()
    return _WasapiStream(
        driver, render, capture, config,
        render_endpoint_id=config.routing.playback_device_id,
        capture_endpoint_id=config.routing.capture_device_id,
        default_render_id=RENDER_EP,
        default_capture_id=CAPTURE_EP,
        **stream_kw)


_STIMULUS = np.linspace(-0.5, 0.5, 16, dtype=np.float64)


# ---------------------------------------------------------------------------
# Channel-mask labels — #876 vocabulary, honest fallback
# ---------------------------------------------------------------------------


class TestChannelMaskLabels:
    def test_stereo_mask(self) -> None:
        assert channel_mask_labels(0x3, 2) == ('FL', 'FR')

    def test_51_mask(self) -> None:
        labels = channel_mask_labels(0x3F, 6)
        assert labels == ('FL', 'FR', 'C', 'LFE', 'SBL', 'SBR')

    def test_71_mask_includes_sides(self) -> None:
        labels = channel_mask_labels(0x63F, 8)
        assert labels == ('FL', 'FR', 'C', 'LFE', 'SBL', 'SBR',
                          'SL', 'SR')

    def test_empty_mask_labels_channels_honestly(self) -> None:
        # No declared mask: channels are ch<N>, never guessed as FL/FR.
        assert channel_mask_labels(0, 2) == ('ch0', 'ch1')

    def test_mask_with_fewer_bits_than_channels(self) -> None:
        labels = channel_mask_labels(0x1, 3)
        assert labels == ('FL', 'ch1', 'ch2')

    def test_stereo_pair_found(self) -> None:
        labels = channel_mask_labels(0x3F, 6)
        assert stereo_pair_channels(labels) == (0, 1)

    def test_stereo_pair_absent(self) -> None:
        labels = ('C', 'LFE', 'SBC')
        assert stereo_pair_channels(labels) is None

    def test_side_pair_preferred_after_front(self) -> None:
        labels = ('FL', 'C', 'SL', 'SR')
        assert stereo_pair_channels(labels) == (2, 3)

    def test_pairs_parity_with_layout_vocabulary(self) -> None:
        try:
            from htdt.cad_layout_tools import CANONICAL_SPEAKER_PAIRS
        except Exception:
            pytest.skip('layout tools not importable in this env')
        from htdt.cad_wasapi_io import _CANONICAL_PAIRS
        assert _CANONICAL_PAIRS == tuple(CANONICAL_SPEAKER_PAIRS.items())


# ---------------------------------------------------------------------------
# Availability staging — precise reasons, never a bare 'unavailable'
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_non_windows_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, 'platform', 'linux')
        backend = WasapiAudioBackend()
        assert backend.available() is False
        assert 'windows' in backend.unavailable_reason().lower()

    def test_generic_driver_error_still_precise(self) -> None:
        # A non-WasapiDriverError failure must still surface as a reason,
        # never a crash through available().
        class _Flaky(ScriptedWasapiDriver):
            def enumerate_endpoints(self):
                raise RuntimeError('com exploded')

        backend = _backend(_Flaky(endpoints=_duplex_endpoints()))
        assert backend.available() is False
        assert 'enumeration failed' in backend.unavailable_reason()

    def test_enumeration_error_is_precise(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            enumerate_error=WasapiDriverError('enumerator lost',
                                              hresult=-2004287484)))
        assert backend.available() is False
        reason = backend.unavailable_reason()
        assert 'enumeration failed' in reason
        assert '0x88890004' in reason

    def test_no_endpoints(self) -> None:
        backend = _backend(ScriptedWasapiDriver(endpoints=()))
        assert backend.available() is False
        reason = backend.unavailable_reason()
        assert 'no active wasapi endpoints' in reason.lower()
        assert 'audio service' in reason.lower()

    def test_render_only(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=(_ep(),)))
        assert backend.available() is False
        assert 'no active wasapi capture endpoint' in (
            backend.unavailable_reason().lower())

    def test_capture_only(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=(_ep(endpoint_id=CAPTURE_EP,
                          data_flow='capture'),)))
        assert backend.available() is False
        assert 'no active wasapi render endpoint' in (
            backend.unavailable_reason().lower())

    def test_full_duplex_available(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        assert backend.available() is True

    def test_unavailable_reason_never_bare(self) -> None:
        backend = _backend(ScriptedWasapiDriver(endpoints=()))
        backend.available()
        reason = backend.unavailable_reason()
        assert reason
        assert reason != 'unavailable'


# ---------------------------------------------------------------------------
# Enumeration mapping — honest AudioDeviceInfo
# ---------------------------------------------------------------------------


class TestEnumeration:
    def test_maps_render_and_capture(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        devices = backend.enumerate_devices()
        assert len(devices) == 2
        render = next(d for d in devices if d.direction == 'playback')
        capture = next(d for d in devices if d.direction == 'capture')
        assert render.device_id == RENDER_EP
        assert render.display_name == 'Test Render Endpoint'
        assert render.max_output_channels == 2
        assert render.max_input_channels == 0
        assert capture.max_input_channels == 2
        assert capture.max_output_channels == 0
        assert render.is_default and capture.is_default
        # Shared mode negotiates exactly the mix rate — nothing else.
        assert render.supported_sample_rates == (48000,)
        assert 'float32' in render.supported_formats
        # Real channel mask + #876 labels are exposed.
        assert render.channel_mask == 0x3
        assert render.channel_labels == ('FL', 'FR')

    def test_endpoint_without_caps_enumerated_honestly(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=(_ep(mix_sample_rate_hz=0, mix_channels=0,
                           supports_float32=False),)))
        devices = backend.enumerate_devices()
        assert len(devices) == 1
        assert devices[0].supported_sample_rates == ()
        assert devices[0].supported_formats == ()


# ---------------------------------------------------------------------------
# open_stream — validation order mirrors the fake backend contract
# ---------------------------------------------------------------------------


class TestOpenStream:
    def test_unavailable_backend_raises_unavailable(self) -> None:
        backend = _backend(ScriptedWasapiDriver(endpoints=()))
        with pytest.raises(BackendUnavailableError) as exc:
            backend.open_stream(_config())
        assert 'no active wasapi endpoints' in str(exc.value).lower()

    def test_unknown_playback_device(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        with pytest.raises(DeviceNotFoundError):
            backend.open_stream(_config(
                routing=_routing(playback_device_id='nope')))

    def test_wrong_direction_rejected(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        with pytest.raises(UnsupportedConfigurationError) as exc:
            backend.open_stream(_config(
                routing=_routing(playback_device_id=CAPTURE_EP)))
        assert 'no playback direction' in str(exc.value)

    def test_channel_out_of_range(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config(
                routing=_routing(playback_channel=9)))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config(
                routing=_routing(capture_channel=9)))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config(
                routing=_routing(loopback_input_channel=9)))

    def test_rate_must_match_mix_rate(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        with pytest.raises(UnsupportedConfigurationError) as exc:
            backend.open_stream(_config(sample_rate_hz=44100))
        assert '48000' in str(exc.value)

    def test_format_rejected(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints()))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config(sample_format='int16'))

    def test_no_float32_support_rejected(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(supports_float32=False)))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config())

    def test_busy_endpoint_is_unavailable_with_action(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(),
            render_error=WasapiBusyError(
                'endpoint held by an exclusive-mode stream — close the '
                'owning application or select another endpoint')))
        with pytest.raises(BackendUnavailableError) as exc:
            backend.open_stream(_config())
        assert 'exclusive-mode' in str(exc.value)

    def test_gone_endpoint_is_unavailable(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(),
            render_error=WasapiEndpointGoneError('endpoint vanished')))
        with pytest.raises(BackendUnavailableError):
            backend.open_stream(_config())

    def test_bind_format_rejection_is_unsupported(self) -> None:
        backend = _backend(ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(),
            render_error=WasapiUnsupportedFormatError('rejected')))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(_config())

    def test_capture_open_failure_closes_render(self) -> None:
        render = _FakeRenderClient()
        driver = ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(),
            render_client=render,
            capture_error=WasapiBusyError('capture busy'))
        backend = _backend(driver)
        with pytest.raises(BackendUnavailableError):
            backend.open_stream(_config())
        assert render.closed is True

    def test_successful_bind_returns_stream(self) -> None:
        driver = ScriptedWasapiDriver(endpoints=_duplex_endpoints())
        backend = _backend(driver)
        stream = backend.open_stream(_config())
        assert isinstance(stream, _WasapiStream)
        assert driver.opened == [
            ('render', RENDER_EP, 48000, 2),
            ('capture', CAPTURE_EP, 48000, 2)]


# ---------------------------------------------------------------------------
# _WasapiStream pump loop — outcome honesty
# ---------------------------------------------------------------------------


class TestPumpLoop:
    def test_completed_run_records_plan(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        expected = 2 * 16 + 2  # reps=2, block=16, gap=2
        # Loopback echo of rendered output on both captured columns.
        capture = _ScriptedCaptureClient(
            channels=2, packet_frames=8,
            loopback_of=render, loopback_source_channel=0,
            loopback_dest_columns=(0, 1))
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert result.outcome == 'completed'
        assert result.expected_frames == expected
        assert result.recorded_frames == expected
        assert result.actual_sample_format == 'float32'
        assert result.captured_channels == (0, 1)
        # Render leg wrote only the bound channel; other channel silent.
        written = render.written
        assert written.shape == (expected, 2)
        plan = np.concatenate(
            [_STIMULUS, np.zeros(2), _STIMULUS, np.zeros(0)])
        assert np.allclose(written[:, 0], plan)
        assert np.all(written[:, 1] == 0.0)
        # Loopback captured the rendered plan.
        assert np.allclose(result.samples[:, 0], plan)
        assert result.xrun_count == 0
        assert result.clipped_samples == 0
        assert render.started and render.stopped and render.closed
        assert capture.started and capture.stopped and capture.closed

    def test_cancel_outcome(self) -> None:
        calls = iter(range(100))
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=4,
            calls_per_packet=8)
        stream = _stream(render=render, capture=capture)

        def cancel() -> bool:
            return next(calls) >= 2

        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=cancel)
        assert result.outcome == 'cancelled'

    def test_default_endpoint_change_is_device_lost(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=4)
        driver = ScriptedWasapiDriver(
            default_changes={'render': ['other-render-0']})
        stream = _stream(render=render, capture=capture, driver=driver)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert result.outcome == 'device_lost'
        assert any(e.kind == 'device_event' and 'render' in e.detail
                   for e in result.events)

    def test_endpoint_gone_mid_run_is_device_lost(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=4,
            raise_after=2,
            raise_with=WasapiEndpointGoneError('gone'))
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert result.outcome == 'device_lost'
        assert any('invalidated' in e.detail for e in result.events)

    def test_discontinuity_flag_counts_xrun(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=8,
            flags_at={2: AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY})
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert result.outcome == 'completed'
        assert result.xrun_count >= 1
        assert any(e.kind == 'xrun' for e in result.events)

    def test_timestamp_error_reports_drift(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=8,
            flags_at={1: AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR})
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert any(e.kind == 'drift' for e in result.events)

    def test_clipped_samples_counted(self) -> None:
        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        signal = np.zeros((4096, 2), dtype=np.float32)
        signal[40:44, 0] = 1.0
        capture = _ScriptedCaptureClient(signal, packet_frames=8)
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        assert result.clipped_samples == 4
        assert sum(e.kind == 'clip' for e in result.events) == 1

    def test_deadline_truncates(self) -> None:
        clock = [0.0]

        def now() -> float:
            clock[0] += 1.0
            return clock[0]

        render = _FakeRenderClient(buffer_frames=64, drain_per_call=64)
        capture = _ScriptedCaptureClient(
            np.zeros((4096, 2), dtype=np.float32), packet_frames=2,
            calls_per_packet=4)
        stream = _stream(render=render, capture=capture,
                         stall_timeout_s=0.001, pump_sleep_s=0.0,
                         now=now)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=4, gap_samples=4,
            should_cancel=lambda: False)
        assert result.truncated is True
        assert result.recorded_frames < result.expected_frames

    def test_underflow_reported_once(self) -> None:
        # Drain faster than writes refill → render starves → underflow.
        # Capture pacing keeps the run alive across several iterations.
        render = _FakeRenderClient(buffer_frames=32, drain_per_call=64)
        expected = 2 * 16 + 2
        signal = np.zeros((expected, 2), dtype=np.float32)
        capture = _ScriptedCaptureClient(signal, packet_frames=8,
                                         calls_per_packet=4)
        stream = _stream(render=render, capture=capture)
        result = stream.run_acquisition(
            _STIMULUS, repetitions=2, gap_samples=2,
            should_cancel=lambda: False)
        underflows = [e for e in result.events if e.kind == 'underflow']
        assert len(underflows) == 1
        assert result.xrun_count >= 1


# ---------------------------------------------------------------------------
# Engine integration — the real backend drives a full run to 'completed'
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    def test_full_run_completes_not_simulated(self) -> None:
        render = _FakeRenderClient(buffer_frames=512,
                                   drain_per_call=512)
        capture = _ScriptedCaptureClient(
            channels=2, packet_frames=256,
            loopback_of=render, loopback_source_channel=0,
            loopback_dest_columns=(0, 1))
        driver = ScriptedWasapiDriver(
            endpoints=_duplex_endpoints(),
            render_client=render, capture_client=capture)
        engine = MeasurementAcquisitionEngine(
            _backend(driver))
        request = _request(routing=_routing())
        report = engine.configure(request)
        assert report.ok, report.blocked_reasons
        engine.arm(_confirmation(request))
        result = engine.start()
        assert engine.stage == 'completed'
        assert result.capture is not None
        assert result.capture.outcome == 'completed'
        assert result.capture.actual_sample_format == 'float32'
        # The engine's timing resolver can qualify via the wired loopback.
        assert result.timing is not None
        # This is the REAL backend — never simulated evidence.
        from htdt.cad_sweep_acquisition import _FAKE_BACKEND_ID
        assert engine.backend.backend_id != _FAKE_BACKEND_ID
        assert engine.backend.backend_id == 'wasapi-audio-io'


# ---------------------------------------------------------------------------
# Smoke test — real endpoints only when the box actually exposes them
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != 'win32', reason='wasapi needs Windows')
class TestRealDriverSmoke:
    def test_real_backend_reports_honest_state(self) -> None:
        backend = WasapiAudioBackend()
        if not backend.available():
            # No endpoints on this box (Windows Audio stopped or no
            # hardware) — the reason must still be precise, never bare.
            reason = backend.unavailable_reason()
            assert reason and reason != 'unavailable'
            assert backend.enumerate_devices() == ()
            pytest.skip(f'no real endpoints: {reason}')
        devices = backend.enumerate_devices()
        renders = [d for d in devices if d.direction == 'playback']
        captures = [d for d in devices if d.direction == 'capture']
        assert renders and captures
        assert all(d.supported_sample_rates for d in devices)
