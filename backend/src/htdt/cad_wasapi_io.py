"""WASAPI shared-mode audio I/O for the #869 native sweep engine (REV70).

Two honest layers:

- A narrow driver seam (:class:`WasapiDriverBase`,
  :class:`WasapiRenderClientBase`, :class:`WasapiCaptureClientBase`) that
  exposes endpoint enumeration plus shared-mode render/capture clients.
  :class:`CtypesWasapiDriver` implements it over raw COM/ctypes —
  IMMDeviceEnumerator -> IAudioClient with no third-party audio library —
  and raises typed :class:`WasapiDriverError` subclasses rather than ever
  inventing success. Tests inject a fake driver on the same seam, so every
  non-hardware path (enumeration mapping, channel-mask labels, config
  negotiation, the pump loop's outcome handling) is exercised without
  audio hardware.
- ``_WasapiStream`` — the pump loop behind
  ``WasapiAudioBackend.open_stream``: pre-fill, capture-first start,
  render writes on the bound channel only, packet drain with
  AUDCLNT_BUFFERFLAGS_* mapped to honest :class:`CaptureEvent`s, and
  device-loss / default-endpoint-change / stall handling that ends the
  run instead of papering over it.

Speaker-position channel masks map onto the #621/#876 logical-channel
vocabulary ('FL', 'FR', 'C', 'LFE', 'SL', 'SR', 'SBL', 'SBR', 'TFL', ...)
via :func:`channel_mask_labels` and :func:`stereo_pair_channels` — the
device mask is the truth, and a mask-less channel is labelled 'ch<N>',
never guessed.

Everything in this module is import-safe on any platform; the ctypes
driver only exists on Windows and only when constructed.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

import numpy as np

from .cad_sweep_acquisition import (
    AcquisitionStream,
    AudioStreamConfig,
    CaptureEvent,
    CaptureOutcome,
    CaptureResult,
)


# ---------------------------------------------------------------------------
# Speaker-position channel masks -> #876 logical-channel vocabulary
# (KSAUDIO_CHANNELMASK bits, per Windows WAVEFORMATEXTENSIBLE.dwChannelMask)
# ---------------------------------------------------------------------------

SPEAKER_FRONT_LEFT = 0x1
SPEAKER_FRONT_RIGHT = 0x2
SPEAKER_FRONT_CENTER = 0x4
SPEAKER_LOW_FREQUENCY = 0x8
SPEAKER_BACK_LEFT = 0x10
SPEAKER_BACK_RIGHT = 0x20
SPEAKER_FRONT_LEFT_OF_CENTER = 0x40
SPEAKER_FRONT_RIGHT_OF_CENTER = 0x80
SPEAKER_BACK_CENTER = 0x100
SPEAKER_SIDE_LEFT = 0x200
SPEAKER_SIDE_RIGHT = 0x400
SPEAKER_TOP_CENTER = 0x800
SPEAKER_TOP_FRONT_LEFT = 0x1000
SPEAKER_TOP_FRONT_CENTER = 0x2000
SPEAKER_TOP_FRONT_RIGHT = 0x4000
SPEAKER_TOP_BACK_LEFT = 0x8000
SPEAKER_TOP_BACK_CENTER = 0x10000
SPEAKER_TOP_BACK_RIGHT = 0x20000

# Mask bit -> logical channel label, in bit order == channel order.
# Labels reuse the layout/identity-chain vocabulary ('FL', 'TFL', ...) so
# a stereo pair picked off a mask never invents a parallel naming scheme.
SPEAKER_BIT_LABELS: tuple[tuple[int, str], ...] = (
    (SPEAKER_FRONT_LEFT, 'FL'),
    (SPEAKER_FRONT_RIGHT, 'FR'),
    (SPEAKER_FRONT_CENTER, 'C'),
    (SPEAKER_LOW_FREQUENCY, 'LFE'),
    (SPEAKER_BACK_LEFT, 'SBL'),
    (SPEAKER_BACK_RIGHT, 'SBR'),
    (SPEAKER_FRONT_LEFT_OF_CENTER, 'FLC'),
    (SPEAKER_FRONT_RIGHT_OF_CENTER, 'FRC'),
    (SPEAKER_BACK_CENTER, 'SBC'),
    (SPEAKER_SIDE_LEFT, 'SL'),
    (SPEAKER_SIDE_RIGHT, 'SR'),
    (SPEAKER_TOP_CENTER, 'TC'),
    (SPEAKER_TOP_FRONT_LEFT, 'TFL'),
    (SPEAKER_TOP_FRONT_CENTER, 'TFC'),
    (SPEAKER_TOP_FRONT_RIGHT, 'TFR'),
    (SPEAKER_TOP_BACK_LEFT, 'TRL'),
    (SPEAKER_TOP_BACK_CENTER, 'TRC'),
    (SPEAKER_TOP_BACK_RIGHT, 'TRR'),
)


def channel_mask_labels(mask: int, channels: int) -> tuple[str, ...]:
    """Per-channel-index labels from a WAVEFORMATEXTENSIBLE channel mask.

    WASAPI binds mask bits to channels in increasing bit order: channel 0
    carries the lowest set bit. A mask that is empty or covers fewer
    channels than the stream reports honest 'ch<N>' placeholders — the
    layout is UNKNOWN, not silently stereo.
    """

    bit_order = [label for bit, label in SPEAKER_BIT_LABELS if mask & bit]
    labels: list[str] = []
    for idx in range(channels):
        labels.append(bit_order[idx] if idx < len(bit_order) else f'ch{idx}')
    return tuple(labels)


# Ordered mirror of cad_layout_tools.CANONICAL_SPEAKER_PAIRS — kept
# inline because this module must stay import-safe on lean hosts (the
# layout module transitively requires the geometry stack). A unit test
# asserts parity with the canonical table whenever it is importable.
_CANONICAL_PAIRS: tuple[tuple[str, str], ...] = (
    ('FL', 'FR'),
    ('SL', 'SR'),
    ('SBL', 'SBR'),
    ('TFL', 'TFR'),
    ('TML', 'TMR'),
    ('TRL', 'TRR'),
    ('L', 'R'),
)


def stereo_pair_channels(labels: Sequence[str]) -> tuple[int, int] | None:
    """Channel indices of the first canonical stereo pair in ``labels``.

    Uses the #621 layout canonical pair vocabulary (FL/FR, SL/SR, SBL/SBR,
    TFL/TFR, TML/TMR, TRL/TRR, L/R) — a stereo pair is only reported when
    the declared mask actually carries both sides.
    """

    try:
        from .cad_layout_tools import CANONICAL_SPEAKER_PAIRS
        pairs: Sequence[tuple[str, str]] = tuple(
            CANONICAL_SPEAKER_PAIRS.items())
    except Exception:
        pairs = _CANONICAL_PAIRS
    for left, right in pairs:
        if left in labels and right in labels:
            return (labels.index(left), labels.index(right))
    return None


# ---------------------------------------------------------------------------
# Driver seam — endpoint descriptors and the render/capture client protocol
# ---------------------------------------------------------------------------


class WasapiDriverError(RuntimeError):
    """A WASAPI/COM call failed. Carries the raw HRESULT when known."""

    def __init__(self, message: str, hresult: int | None = None):
        self.hresult = hresult
        detail = message if hresult is None else (
            f'{message} (HRESULT 0x{hresult & 0xFFFFFFFF:08X})')
        super().__init__(detail)


class WasapiEndpointGoneError(WasapiDriverError):
    """AUDCLNT_E_DEVICE_INVALIDATED / RESOURCES_INVALIDATED or a vanished
    endpoint — the device is no longer usable mid-session."""


class WasapiUnsupportedFormatError(WasapiDriverError):
    """The endpoint rejected the requested shared-mode format."""


class WasapiBusyError(WasapiDriverError):
    """The endpoint is held by an exclusive-mode stream."""


@dataclass(frozen=True)
class WasapiEndpointInfo:
    """One active WASAPI endpoint as the driver reports it."""

    endpoint_id: str
    friendly_name: str
    data_flow: Literal['render', 'capture']
    is_default: bool
    mix_sample_rate_hz: int
    mix_channels: int
    mix_channel_mask: int
    mix_format_tag: int          # 1=PCM int, 3=IEEE float, 0 when unknown
    supports_float32: bool      # probed via IsFormatSupported when possible


class WasapiRenderClientBase:
    """One bound shared-mode render client (the playback leg)."""

    channels: int = 0
    sample_rate_hz: int = 0
    buffer_frames: int = 0

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def free_space_frames(self) -> int:
        raise NotImplementedError

    def write_f32(self, frames: np.ndarray) -> int:
        """Interleaved (frames, channels) float32 -> frames accepted."""
        raise NotImplementedError


class WasapiCaptureClientBase:
    """One bound shared-mode capture client (the recording leg)."""

    channels: int = 0
    sample_rate_hz: int = 0

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def read_packet(self) -> tuple[np.ndarray | None, int, int | None]:
        """(frames x channels float32, AUDCLNT_BUFFERFLAGS_*, device
        position in frames) — frames None when no packet is pending."""
        raise NotImplementedError


class WasapiDriverBase:
    """The injected seam: enumerate, default-endpoint, open clients."""

    def enumerate_endpoints(self) -> tuple[WasapiEndpointInfo, ...]:
        raise NotImplementedError

    def default_endpoint_id(self, data_flow: str) -> str | None:
        raise NotImplementedError

    def open_render(
        self, endpoint_id: str, sample_rate_hz: int, channels: int,
    ) -> WasapiRenderClientBase:
        raise NotImplementedError

    def open_capture(
        self, endpoint_id: str, sample_rate_hz: int, channels: int,
    ) -> WasapiCaptureClientBase:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Capture packet flags (AUDCLNT_BUFFERFLAGS_*)
# ---------------------------------------------------------------------------

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4

#: Samples at |x| >= this are reported as clipped — a hair below float32
#: full scale so int24 max (1 - 2**-23) counts and ordinary loud-but-legal
#: material does not.
_CLIP_THRESHOLD_F32 = 0.9999999


# ---------------------------------------------------------------------------
# _WasapiStream — the pump loop behind WasapiAudioBackend.open_stream
# ---------------------------------------------------------------------------


class _WasapiStream(AcquisitionStream):
    """Simultaneous shared-mode render+capture against one driver pair.

    Records exactly the playback plan (repetitions x (block + gap) - gap)
    of capture frames — identical semantics to the deterministic fake so
    the evidence path stays uniform. Capture starts before render so the
    recording honestly contains pre-onset room noise.
    """

    def __init__(
        self,
        driver: WasapiDriverBase,
        render: WasapiRenderClientBase,
        capture: WasapiCaptureClientBase,
        config: AudioStreamConfig,
        *,
        render_endpoint_id: str,
        capture_endpoint_id: str,
        default_render_id: str | None,
        default_capture_id: str | None,
        stall_timeout_s: float = 8.0,
        pump_sleep_s: float = 0.005,
        default_poll_interval_s: float = 0.25,
        now: Callable[[], float] = time.monotonic,
    ):
        self._driver = driver
        self._render = render
        self._capture = capture
        self._config = config
        self._render_endpoint_id = render_endpoint_id
        self._capture_endpoint_id = capture_endpoint_id
        self._default_at_open = {
            'render': default_render_id, 'capture': default_capture_id}
        self._stall_timeout_s = stall_timeout_s
        self._pump_sleep_s = pump_sleep_s
        self._default_poll_interval_s = default_poll_interval_s
        self._now = now

    def run_acquisition(
        self,
        stimulus_block: np.ndarray,
        repetitions: int,
        gap_samples: int,
        should_cancel: Callable[[], bool],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> CaptureResult:
        routing = self._config.routing
        block = np.asarray(stimulus_block, dtype=np.float64)
        rep_frames = len(block)
        expected = repetitions * rep_frames + (repetitions - 1) * gap_samples
        render_ch = self._render.channels
        cap_columns = (routing.capture_channel,) + (
            (routing.loopback_input_channel,)
            if routing.loopback_input_channel is not None else ()
        )

        chunks: list[np.ndarray] = []
        events: list[CaptureEvent] = []
        recorded = 0
        written = 0
        xruns = 0
        clipped = 0
        clip_announced = False
        outcome: CaptureOutcome = 'completed'
        truncated = False
        starved = False

        def _src_frame(i: int) -> float:
            off = i % (rep_frames + gap_samples)
            return block[off] if off < rep_frames else 0.0

        def _render_chunk(n: int) -> np.ndarray:
            buf = np.zeros((n, render_ch), dtype=np.float32)
            idx = np.arange(written, written + n)
            buf[:, routing.playback_channel] = np.asarray(
                [_src_frame(i) for i in idx], dtype=np.float32)
            return buf

        def _drain_capture() -> bool:
            nonlocal recorded, xruns, clipped, clip_announced
            got = False
            while True:
                packet, flags, _pos = self._capture.read_packet()
                if packet is None or packet.shape[0] == 0:
                    return got
                got = True
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    # Buffer content is undefined for silent packets —
                    # substitute zeros rather than recording garbage.
                    frames = np.zeros_like(
                        packet, dtype=np.float64)
                else:
                    frames = np.asarray(packet, dtype=np.float64)
                if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    xruns += 1
                    events.append(CaptureEvent(
                        kind='xrun', at_frame=recorded,
                        detail='capture packet flagged DATA_DISCONTINUITY'))
                if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR:
                    events.append(CaptureEvent(
                        kind='drift', at_frame=recorded,
                        detail='capture device timestamp error reported'))
                cols = frames[:, [c for c in cap_columns]]
                over = np.abs(cols) >= _CLIP_THRESHOLD_F32
                n_clip = int(np.count_nonzero(over))
                if n_clip:
                    clipped += n_clip
                    if not clip_announced:
                        clip_announced = True
                        events.append(CaptureEvent(
                            kind='clip', at_frame=recorded,
                            detail='captured signal reached full scale'))
                chunks.append(cols)
                recorded += cols.shape[0]
                if on_progress is not None:
                    on_progress(recorded, expected)

        def _defaults_changed() -> str | None:
            for flow in ('render', 'capture'):
                if self._driver.default_endpoint_id(flow) != (
                        self._default_at_open[flow]):
                    return flow
            return None

        started = self._now()
        deadline = started + max(
            self._stall_timeout_s,
            1.5 * (expected / float(self._capture.sample_rate_hz or 1)) + 5.0)

        try:
            # Capture first so the recording honestly leads the playback
            # onset (pre-roll silence feeds the noise-floor estimate).
            self._capture.start()
            try:
                # Pre-fill the render buffer before starting playback so the
                # first callback never starves.
                space = self._render.free_space_frames()
                preload = min(space, expected - written)
                if preload > 0:
                    written += self._render.write_f32(_render_chunk(preload))
                self._render.start()
            except Exception:
                self._capture.stop()
                raise

            next_default_poll = (
                self._now() + self._default_poll_interval_s)
            while recorded < expected:
                if should_cancel():
                    outcome = 'cancelled'
                    break
                changed = _defaults_changed()
                if changed is not None:
                    outcome = 'device_lost'
                    events.append(CaptureEvent(
                        kind='device_event', at_frame=recorded,
                        detail=f'default {changed} endpoint changed '
                               'mid-run'))
                    break
                _drain_capture()
                if recorded >= expected:
                    break

                # Render leg: feed the device as space frees.
                if written < expected:
                    space = self._render.free_space_frames()
                    if (space >= self._render.buffer_frames
                            and self._render.buffer_frames > 0
                            and not starved):
                        # Fully drained while data remained: an underrun.
                        starved = True
                        xruns += 1
                        events.append(CaptureEvent(
                            kind='underflow', at_frame=recorded,
                            detail='render buffer drained mid-plan'))
                    n = min(space, expected - written)
                    if n > 0:
                        written += self._render.write_f32(
                            _render_chunk(n))

                if self._now() > deadline:
                    truncated = recorded < expected
                    events.append(CaptureEvent(
                        kind='device_event', at_frame=recorded,
                        detail='capture deadline exceeded — recording '
                               'ended short'))
                    break
                if self._now() > next_default_poll:
                    next_default_poll = (
                        self._now() + self._default_poll_interval_s)
                time.sleep(self._pump_sleep_s)
        except WasapiEndpointGoneError as exc:
            outcome = 'device_lost'
            events.append(CaptureEvent(
                kind='device_event', at_frame=recorded,
                detail=f'endpoint invalidated mid-run: {exc}'))
        finally:
            for client in (self._render, self._capture):
                try:
                    client.stop()
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass

        samples = (np.concatenate(chunks, axis=0) if chunks else
                   np.zeros((0, len(cap_columns)), dtype=np.float64))
        return CaptureResult(
            outcome=outcome,
            actual_sample_rate_hz=self._capture.sample_rate_hz,
            actual_sample_format='float32',
            captured_channels=cap_columns,
            samples=samples,
            expected_frames=expected,
            recorded_frames=len(samples),
            truncated=truncated or (
                outcome == 'completed' and recorded < expected),
            xrun_count=xruns,
            clipped_samples=clipped,
            events=tuple(events),
        )


__all__ = [
    'AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY',
    'AUDCLNT_BUFFERFLAGS_SILENT',
    'AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR',
    'SPEAKER_BIT_LABELS',
    'WasapiCaptureClientBase',
    'WasapiDriverBase',
    'WasapiDriverError',
    'WasapiBusyError',
    'WasapiEndpointGoneError',
    'WasapiEndpointInfo',
    'WasapiRenderClientBase',
    'WasapiUnsupportedFormatError',
    'channel_mask_labels',
    'stereo_pair_channels',
]

# ---------------------------------------------------------------------------
# CtypesWasapiDriver — real COM/ctypes implementation (Windows only)
# ---------------------------------------------------------------------------

if sys.platform == 'win32':

    import ctypes
    from ctypes import wintypes

    # -- COM plumbing ----------------------------------------------------

    _CLSCTX_ALL = 23
    _COINIT_MULTITHREADED = 0x0
    _STGM_READ = 0

    _E_INVALIDARG = ctypes.c_int32(0x80070057).value
    _E_POINTER = ctypes.c_int32(0x80004003).value
    _E_NOTFOUND = ctypes.c_int32(0x80070490).value
    _RPC_E_CHANGED_MODE = ctypes.c_int32(0x80010106).value
    _CO_E_NOTINITIALIZED = ctypes.c_int32(0x800401F0).value
    _AUDCLNT_S_BUFFER_EMPTY = ctypes.c_int32(0x08890001).value
    _AUDCLNT_E_NOT_INITIALIZED = ctypes.c_int32(0x88890001).value
    _AUDCLNT_E_DEVICE_INVALIDATED = ctypes.c_int32(0x88890004).value
    _AUDCLNT_E_UNSUPPORTED_FORMAT = ctypes.c_int32(0x88890008).value
    _AUDCLNT_E_DEVICE_IN_USE = ctypes.c_int32(0x8889000A).value
    _AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED = ctypes.c_int32(0x8889000E).value
    _AUDCLNT_E_RESOURCES_INVALIDATED = ctypes.c_int32(0x88890026).value

    _GONE_HRESULTS = frozenset({
        _AUDCLNT_E_DEVICE_INVALIDATED,
        _AUDCLNT_E_RESOURCES_INVALIDATED,
        _E_NOTFOUND,
    })

    class _GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                    ('Data3', ctypes.c_uint16),
                    ('Data4', ctypes.c_ubyte * 8)]

    def _guid(text: str) -> _GUID:
        parts = text.strip('{}').split('-')
        g = _GUID()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        tail = parts[3] + parts[4]
        for i in range(8):
            g.Data4[i] = int(tail[i * 2:i * 2 + 2], 16)
        return g

    _CLSID_MMDEVICE_ENUMERATOR = _guid('BCDE0395-E52F-467C-8E3D-C4579291692E')
    _IID_IMM_DEVICE_ENUMERATOR = _guid('A95664D2-9614-4F35-A746-DE8DB63617E6')
    _IID_IMM_DEVICE = _guid('D666063F-1587-4E43-81F1-B948E807363F')
    _IID_IAUDIO_CLIENT = _guid('1CB9AD4C-DBFA-4C32-B178-C2F568A703B2')
    _IID_IAUDIO_RENDER_CLIENT = _guid('F294ACFC-3146-4483-A7BF-ADDCA7C260E2')
    _IID_IAUDIO_CAPTURE_CLIENT = _guid('C8ADBD64-E71E-48A0-A4DE-185C395CD317')
    _IID_IPROPERTY_STORE = _guid('886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99')
    _PKEY_DEVICE_FRIENDLY_NAME = (
        _guid('A45C254E-DF1C-4EFD-8020-67D146A850E0'), 14)
    _PKEY_DEVICEINTERFACE_FRIENDLY_NAME = (
        _guid('026E516E-B814-414B-83CD-856D6FEF4822'), 2)

    _E_RENDER = 0
    _E_CAPTURE = 1
    _DEVICE_STATE_ACTIVE = 0x1

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ('wFormatTag', wintypes.WORD),
            ('nChannels', wintypes.WORD),
            ('nSamplesPerSec', wintypes.DWORD),
            ('nAvgBytesPerSec', wintypes.DWORD),
            ('nBlockAlign', wintypes.WORD),
            ('wBitsPerSample', wintypes.WORD),
            ('cbSize', wintypes.WORD),
        ]

    _WAVE_FORMAT_PCM = 0x0001
    _WAVE_FORMAT_IEEE_FLOAT = 0x0003
    _WAVE_FORMAT_EXTENSIBLE = 0xFFFE

    def _float32_format(sample_rate_hz: int, channels: int) -> _WAVEFORMATEX:
        fmt = _WAVEFORMATEX()
        fmt.wFormatTag = _WAVE_FORMAT_IEEE_FLOAT
        fmt.nChannels = channels
        fmt.nSamplesPerSec = sample_rate_hz
        fmt.nBlockAlign = channels * 4
        fmt.nAvgBytesPerSec = sample_rate_hz * channels * 4
        fmt.wBitsPerSample = 32
        fmt.cbSize = 0
        return fmt

    def _vtbl(ppv: int):
        base = ctypes.cast(
            ctypes.c_void_p(ppv), ctypes.POINTER(ctypes.c_void_p))[0]
        return ctypes.cast(base, ctypes.POINTER(ctypes.c_void_p))

    def _release(ppv) -> None:
        if not ppv:
            return
        fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            _vtbl(int(ppv))[2])
        fn(ctypes.c_void_p(int(ppv)))

    def _check(hr: int, what: str) -> int:
        if hr < 0:
            if hr in _GONE_HRESULTS:
                raise WasapiEndpointGoneError(f'{what} failed', hresult=hr)
            if hr == _AUDCLNT_E_UNSUPPORTED_FORMAT:
                raise WasapiUnsupportedFormatError(
                    f'{what} failed', hresult=hr)
            if hr in (_AUDCLNT_E_DEVICE_IN_USE,
                      _AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED):
                raise WasapiBusyError(f'{what} failed', hresult=hr)
            raise WasapiDriverError(f'{what} failed', hresult=hr)
        return hr

    class CtypesWasapiDriver(WasapiDriverBase):
        """Real WASAPI driver over ctypes COM.

        Only constructible on Windows; every public method re-asserts COM
        init so calls are honest on whichever thread the engine uses.
        """

        def __init__(self):
            self._ole32 = ctypes.windll.ole32
            self._oleaut32 = ctypes.windll.oleaut32
            self._ole32.CoInitializeEx.argtypes = [
                ctypes.c_void_p, wintypes.DWORD]
            self._ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
            self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            self._com_init()
            self._enumerator = self._create_enumerator()

        # -- COM helpers -------------------------------------------------

        def _com_init(self) -> None:
            hr = self._ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
            if hr < 0 and hr != _RPC_E_CHANGED_MODE:
                raise WasapiDriverError(
                    'COM initialization failed on this thread', hresult=hr)

        def _create_enumerator(self):
            ppv = ctypes.c_void_p()
            hr = self._ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR), None, _CLSCTX_ALL,
                ctypes.byref(_IID_IMM_DEVICE_ENUMERATOR), ctypes.byref(ppv))
            if hr < 0 or not ppv.value:
                raise WasapiDriverError(
                    'IMMDeviceEnumerator creation failed — is the Windows '
                    'Audio service running?', hresult=hr)
            return ppv.value

        def _call(self, ppv, index, restype, argtypes, *args):
            fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(
                _vtbl(ppv)[index])
            return fn(ctypes.c_void_p(ppv), *args)

        def _enum_collection(self, data_flow: int):
            coll = ctypes.c_void_p()
            hr = self._call(
                self._enumerator, 3, ctypes.c_int32,
                (ctypes.c_int32, wintypes.DWORD,
                 ctypes.POINTER(ctypes.c_void_p)),
                data_flow, _DEVICE_STATE_ACTIVE, ctypes.byref(coll))
            _check(hr, 'EnumAudioEndpoints')
            return coll.value

        def _collection_count(self, coll) -> int:
            count = wintypes.UINT(0)
            hr = self._call(
                coll, 3, ctypes.c_int32,
                (ctypes.POINTER(wintypes.UINT),), ctypes.byref(count))
            _check(hr, 'IMMDeviceCollection::GetCount')
            return int(count.value)

        def _collection_item(self, coll, index: int):
            dev = ctypes.c_void_p()
            hr = self._call(
                coll, 4, ctypes.c_int32,
                (wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)),
                index, ctypes.byref(dev))
            _check(hr, 'IMMDeviceCollection::Item')
            return dev.value

        def _device_id(self, dev) -> str:
            pstr = ctypes.c_void_p()
            hr = self._call(
                dev, 5, ctypes.c_int32,
                (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(pstr))
            _check(hr, 'IMMDevice::GetId')
            try:
                return ctypes.wstring_at(pstr.value)
            finally:
                self._ole32.CoTaskMemFree(pstr)

        def _device_state(self, dev) -> int:
            state = wintypes.DWORD(0)
            hr = self._call(
                dev, 6, ctypes.c_int32,
                (ctypes.POINTER(wintypes.DWORD),), ctypes.byref(state))
            _check(hr, 'IMMDevice::GetState')
            return int(state.value)

        def _friendly_name(self, dev) -> str:
            store = ctypes.c_void_p()
            hr = self._call(
                dev, 4, ctypes.c_int32,
                (wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)),
                _STGM_READ, ctypes.byref(store))
            if hr < 0 or not store.value:
                return ''
            try:
                for fmtid, pid in (_PKEY_DEVICE_FRIENDLY_NAME,
                                 _PKEY_DEVICEINTERFACE_FRIENDLY_NAME):
                    key = _PROPERTYKEY(fmtid, pid)
                    pv = ctypes.create_string_buffer(32)
                    hr = self._call(
                        store.value, 5, ctypes.c_int32,
                        (ctypes.POINTER(_PROPERTYKEY), ctypes.c_void_p),
                        ctypes.byref(key), pv)
                    if hr == 0:
                        vt = wintypes.WORD.from_address(
                            ctypes.addressof(pv)).value
                        if vt in (31, 30):  # VT_LPWSTR / VT_LPSTR
                            ptr = ctypes.c_void_p.from_address(
                                ctypes.addressof(pv) + 8).value
                            if ptr:
                                if vt == 31:
                                    return ctypes.wstring_at(ptr)
                                return ctypes.string_at(ptr).decode(
                                    'mbcs', errors='replace')
                        self._oleaut32.VariantClear(pv)
                return ''
            finally:
                _release(store.value)

        def _activate_audio_client(self, dev):
            client = ctypes.c_void_p()
            hr = self._call(
                dev, 3, ctypes.c_int32,
                (ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(_IID_IAUDIO_CLIENT), _CLSCTX_ALL, None,
                ctypes.byref(client))
            _check(hr, 'IMMDevice::Activate(IAudioClient)')
            return client.value

        def _mix_format(self, client):
            """(rate, channels, mask, format_tag) from GetMixFormat."""

            pfmt = ctypes.c_void_p()
            hr = self._call(
                client, 8, ctypes.c_int32,
                (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(pfmt))
            _check(hr, 'IAudioClient::GetMixFormat')
            try:
                fmt = _WAVEFORMATEX.from_address(pfmt.value)
                mask = 0
                tag = int(fmt.wFormatTag)
                if fmt.wFormatTag == _WAVE_FORMAT_EXTENSIBLE and (
                        fmt.cbSize >= 22):
                    mask = int(ctypes.c_uint32.from_address(
                        pfmt.value + 20).value)
                    sub_tag = wintypes.WORD.from_address(
                        pfmt.value + 24).value
                    tag = int(sub_tag)
                return (int(fmt.nSamplesPerSec), int(fmt.nChannels),
                        mask, tag)
            finally:
                self._ole32.CoTaskMemFree(pfmt)

        def _supports_float32(self, client, rate: int, channels: int) -> bool:
            fmt = _float32_format(rate, channels)
            closest = ctypes.c_void_p()
            hr = self._call(
                client, 7, ctypes.c_int32,
                (ctypes.c_int32, ctypes.POINTER(_WAVEFORMATEX),
                 ctypes.POINTER(ctypes.c_void_p)),
                0, ctypes.byref(fmt), ctypes.byref(closest))
            if closest.value:
                self._ole32.CoTaskMemFree(closest)
            return hr == 0

        def _device_for(self, endpoint_id: str):
            dev = ctypes.c_void_p()
            hr = self._call(
                self._enumerator, 5, ctypes.c_int32,
                (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)),
                endpoint_id, ctypes.byref(dev))
            if hr in _GONE_HRESULTS or hr < 0:
                raise WasapiEndpointGoneError(
                    f'endpoint {endpoint_id!r} is not present', hresult=hr)
            return dev.value

        def _default_device(self, data_flow: int):
            dev = ctypes.c_void_p()
            hr = self._call(
                self._enumerator, 4, ctypes.c_int32,
                (ctypes.c_int32, ctypes.c_int32,
                 ctypes.POINTER(ctypes.c_void_p)),
                data_flow, 0, ctypes.byref(dev))
            if hr < 0 or not dev.value:
                return None
            return dev.value

        # -- public seam ---------------------------------------------------

        def enumerate_endpoints(self) -> tuple[WasapiEndpointInfo, ...]:
            self._com_init()
            out: list[WasapiEndpointInfo] = []
            for flow_code, flow in ((_E_RENDER, 'render'),
                                    (_E_CAPTURE, 'capture')):
                default_id = self.default_endpoint_id(flow)
                coll = self._enum_collection(flow_code)
                try:
                    for i in range(self._collection_count(coll)):
                        dev = self._collection_item(coll, i)
                        try:
                            endpoint_id = self._device_id(dev)
                            name = (self._friendly_name(dev)
                                    or endpoint_id)
                            client = None
                            try:
                                client = self._activate_audio_client(dev)
                                rate, ch, mask, tag = self._mix_format(client)
                                f32 = self._supports_float32(
                                    client, rate, ch)
                            except WasapiDriverError:
                                # Endpoint exists but cannot report caps —
                                # enumerate it honestly with unknown caps so
                                # it can never silently bind.
                                rate, ch, mask, tag, f32 = 0, 0, 0, 0, False
                            finally:
                                if client:
                                    _release(client)
                            out.append(WasapiEndpointInfo(
                                endpoint_id=endpoint_id,
                                friendly_name=name,
                                data_flow=flow,
                                is_default=(endpoint_id == default_id),
                                mix_sample_rate_hz=rate,
                                mix_channels=ch,
                                mix_channel_mask=mask,
                                mix_format_tag=tag,
                                supports_float32=f32,
                            ))
                        finally:
                            _release(dev)
                finally:
                    _release(coll)
            return tuple(out)

        def default_endpoint_id(self, data_flow: str) -> str | None:
            self._com_init()
            flow = _E_RENDER if data_flow == 'render' else _E_CAPTURE
            dev = self._default_device(flow)
            if not dev:
                return None
            try:
                return self._device_id(dev)
            except WasapiDriverError:
                return None
            finally:
                _release(dev)

        def _open_client(
            self, endpoint_id: str, sample_rate_hz: int, channels: int,
            service_iid: _GUID, service_name: str,
        ) -> tuple[int, int, int]:
            """Activate + Initialize shared-mode float32 + GetService.

            Returns (audio_client_ptr, service_client_ptr, buffer_frames).
            Raises the typed WasapiDriverError subclasses on failure.
            """

            self._com_init()
            dev = self._device_for(endpoint_id)
            try:
                client = self._activate_audio_client(dev)
            finally:
                _release(dev)
            try:
                fmt = _float32_format(sample_rate_hz, channels)
                hr = self._call(
                    client, 3, ctypes.c_int32,
                    (ctypes.c_int32, wintypes.DWORD, ctypes.c_int64,
                     ctypes.c_int64, ctypes.POINTER(_WAVEFORMATEX),
                     ctypes.c_void_p),
                    0, 0, 500_000, 0, ctypes.byref(fmt), None)
                if hr == _AUDCLNT_E_UNSUPPORTED_FORMAT:
                    raise WasapiUnsupportedFormatError(
                        f'endpoint {endpoint_id!r} rejected shared-mode '
                        f'float32 {channels}ch @ {sample_rate_hz} Hz',
                        hresult=hr)
                if hr in (_AUDCLNT_E_DEVICE_IN_USE,
                          _AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED):
                    raise WasapiBusyError(
                        f'endpoint {endpoint_id!r} is held by an '
                        'exclusive-mode stream — close the owning '
                        'application or select another endpoint',
                        hresult=hr)
                _check(hr, 'IAudioClient::Initialize')
                buffer_frames = wintypes.UINT(0)
                hr = self._call(
                    client, 4, ctypes.c_int32,
                    (ctypes.POINTER(wintypes.UINT),),
                    ctypes.byref(buffer_frames))
                _check(hr, 'IAudioClient::GetBufferSize')
                svc = ctypes.c_void_p()
                hr = self._call(
                    client, 14, ctypes.c_int32,
                    (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
                    ctypes.byref(service_iid), ctypes.byref(svc))
                _check(hr, f'IAudioClient::GetService({service_name})')
                return client, svc.value, int(buffer_frames.value)
            except Exception:
                _release(client)
                raise

        def open_render(
            self, endpoint_id: str, sample_rate_hz: int, channels: int,
        ) -> '_CtypesRenderClient':
            client, svc, buf = self._open_client(
                endpoint_id, sample_rate_hz, channels,
                _IID_IAUDIO_RENDER_CLIENT, 'IAudioRenderClient')
            return _CtypesRenderClient(
                self, client, svc, buf, sample_rate_hz, channels)

        def open_capture(
            self, endpoint_id: str, sample_rate_hz: int, channels: int,
        ) -> '_CtypesCaptureClient':
            client, svc, buf = self._open_client(
                endpoint_id, sample_rate_hz, channels,
                _IID_IAUDIO_CAPTURE_CLIENT, 'IAudioCaptureClient')
            return _CtypesCaptureClient(
                self, client, svc, sample_rate_hz, channels)

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [('fmtid', _GUID), ('pid', wintypes.DWORD)]

    # -- bound client wrappers ----------------------------------------------

    class _CtypesRenderClient(WasapiRenderClientBase):
        def __init__(self, driver: 'CtypesWasapiDriver', audio_client: int,
                     render_client: int, buffer_frames: int,
                     sample_rate_hz: int, channels: int):
            self._driver = driver
            self._client = audio_client
            self._svc = render_client
            self.channels = channels
            self.sample_rate_hz = sample_rate_hz
            self.buffer_frames = buffer_frames
            self._closed = False

        def start(self) -> None:
            self._driver._com_init()
            hr = self._driver._call(
                self._client, 10, ctypes.c_int32, ())
            _check(hr, 'IAudioClient::Start(render)')

        def stop(self) -> None:
            self._driver._com_init()
            hr = self._driver._call(
                self._client, 11, ctypes.c_int32, ())
            _check(hr, 'IAudioClient::Stop(render)')

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            _release(self._svc)
            _release(self._client)

        def free_space_frames(self) -> int:
            self._driver._com_init()
            padding = wintypes.UINT(0)
            hr = self._driver._call(
                self._client, 6, ctypes.c_int32,
                (ctypes.POINTER(wintypes.UINT),), ctypes.byref(padding))
            _check(hr, 'IAudioClient::GetCurrentPadding')
            return max(0, self.buffer_frames - int(padding.value))

        def write_f32(self, frames: np.ndarray) -> int:
            self._driver._com_init()
            n = min(frames.shape[0], self.free_space_frames())
            if n <= 0:
                return 0
            pdata = ctypes.c_void_p()
            hr = self._driver._call(
                self._svc, 3, ctypes.c_int32,
                (wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)),
                n, ctypes.byref(pdata))
            _check(hr, 'IAudioRenderClient::GetBuffer')
            try:
                flat = np.ascontiguousarray(
                    frames[:n], dtype='<f4')
                ctypes.memmove(
                    pdata.value, flat.tobytes(), flat.nbytes)
            finally:
                hr = self._driver._call(
                    self._svc, 4, ctypes.c_int32,
                    (wintypes.DWORD, wintypes.DWORD), n, 0)
                _check(hr, 'IAudioRenderClient::ReleaseBuffer')
            return n

    class _CtypesCaptureClient(WasapiCaptureClientBase):
        def __init__(self, driver: 'CtypesWasapiDriver', audio_client: int,
                     capture_client: int, sample_rate_hz: int,
                     channels: int):
            self._driver = driver
            self._client = audio_client
            self._svc = capture_client
            self.channels = channels
            self.sample_rate_hz = sample_rate_hz
            self._closed = False

        def start(self) -> None:
            self._driver._com_init()
            hr = self._driver._call(
                self._client, 10, ctypes.c_int32, ())
            _check(hr, 'IAudioClient::Start(capture)')

        def stop(self) -> None:
            self._driver._com_init()
            hr = self._driver._call(
                self._client, 11, ctypes.c_int32, ())
            _check(hr, 'IAudioClient::Stop(capture)')

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            _release(self._svc)
            _release(self._client)

        def read_packet(self) -> tuple[np.ndarray | None, int, int | None]:
            self._driver._com_init()
            pdata = ctypes.c_void_p()
            nframes = wintypes.UINT(0)
            flags = wintypes.DWORD(0)
            devpos = ctypes.c_uint64(0)
            qpc = ctypes.c_uint64(0)
            hr = self._driver._call(
                self._svc, 3, ctypes.c_int32,
                (ctypes.POINTER(ctypes.c_void_p),
                 ctypes.POINTER(wintypes.UINT),
                 ctypes.POINTER(wintypes.DWORD),
                 ctypes.POINTER(ctypes.c_uint64),
                 ctypes.POINTER(ctypes.c_uint64)),
                ctypes.byref(pdata), ctypes.byref(nframes),
                ctypes.byref(flags), ctypes.byref(devpos),
                ctypes.byref(qpc))
            if hr == _AUDCLNT_S_BUFFER_EMPTY or nframes.value == 0:
                return None, 0, None
            _check(hr, 'IAudioCaptureClient::GetBuffer')
            try:
                nbytes = nframes.value * self.channels * 4
                raw = ctypes.string_at(pdata.value, nbytes)
                frames = np.frombuffer(raw, dtype='<f4').reshape(
                    -1, self.channels)
                pos = int(devpos.value)
                flag_bits = int(flags.value)
            finally:
                hr = self._driver._call(
                    self._svc, 4, ctypes.c_int32,
                    (wintypes.DWORD,), nframes.value)
                _check(hr, 'IAudioCaptureClient::ReleaseBuffer')
            return frames.copy(), flag_bits, pos

    __all__ += ['CtypesWasapiDriver', '_WasapiStream']
else:
    __all__ += ['_WasapiStream']
