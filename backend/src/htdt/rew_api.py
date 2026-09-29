from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import struct
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from .ingress import IngressTooLargeError, read_response_bounded
from .limits import MAX_REW_API_RESPONSE_BYTES, MAX_REW_ARRAY_SAMPLES, max_base64_chars


DEFAULT_REW_API_URL = 'http://127.0.0.1:4735'
ROOMSIM_ADAPTER_VERSION = 'rew-roomsim-readonly-1'


class RewApiError(RuntimeError):
    pass


class RewApiUnavailable(RewApiError):
    pass


class RewApiNotFound(RewApiError):
    """The entity the request names does not exist upstream.

    Distinct from a transport or shape failure: the REW endpoint answered
    normally but holds no such measurement/mic-position/source, so HTTP
    surfaces should answer 404 rather than a 502 that blames the upstream.
    """


class RewApiResponseTooLarge(RewApiError, IngressTooLargeError):
    """Oversized REW API payload rejected before unbounded allocation.

    Shares the ingress resource-safety policy (htdt.limits) with native file
    import and the legacy browser transport while remaining a RewApiError so
    existing availability/error handling keeps working.
    """


@dataclass(frozen=True)
class RewFrequencyResponse:
    measurement_id: str
    unit: str | None
    smoothing: str | None
    start_frequency_hz: float
    points_per_octave: float | None
    frequency_step_hz: float | None
    frequency_hz: tuple[float, ...]
    magnitude: tuple[float, ...]
    phase_deg: tuple[float, ...] | None
    requested_unit: str
    requested_ppo: int | None
    requested_smoothing: str | None


@dataclass(frozen=True)
class RewFrequencyResponseSnapshot:
    measurement_summary: dict[str, Any]
    query: dict[str, str | int]
    raw_frequency_response: dict[str, Any]
    decoded: RewFrequencyResponse


@dataclass(frozen=True)
class RewRoomSimFrequencyResponse:
    source_name: str | None
    mic_position: str
    message: str | None
    unit: str | None
    smoothing: str | None
    start_frequency_hz: float
    points_per_octave: float | None
    frequency_step_hz: float | None
    frequency_hz: tuple[float, ...]
    magnitude: tuple[float, ...]
    phase_deg: tuple[float, ...] | None


@dataclass(frozen=True)
class RewRoomSimSnapshot:
    rew_version: str
    room_size: dict[str, Any]
    room_is_sealed: bool
    absorptions: dict[str, Any]
    options: dict[str, Any]
    head_position_rew: dict[str, Any]
    head_position_htdt: dict[str, float]
    mic_position_offsets: dict[str, Any]
    active_sources: tuple[str, ...]
    recognized_sources: tuple[str, ...]
    mic_positions: tuple[str, ...]
    sources: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RewEngineSession:
    """Acquisition-session authority for one external measurement engine (#599).

    A persisted REW import binds the exact engine session it ran under: the
    producer identity (``engine_kind``/``engine_version``), the adapter that
    translated it (``adapter_id``/``adapter_version``), the endpoint, the
    per-capability support observed at session open and a semantic seal over
    the whole snapshot. The JSON shape of the localhost API is not the
    interface — this sealed record is.
    """

    engine_session_id: str
    engine_kind: str
    engine_version: str | None
    adapter_id: str
    adapter_version: str
    endpoint: str
    capability_snapshot: dict[str, Any]
    observed_at_utc: str
    semantic_sha256: str

    def payload(self) -> dict[str, Any]:
        """The sealed semantic snapshot, for provenance merge into a record."""
        return asdict(self)


def validate_rew_api_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != 'http':
        raise ValueError('REW API URL must use http')
    if parsed.hostname not in {'127.0.0.1', 'localhost'}:
        raise ValueError('REW API is restricted to localhost')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('REW API URL must not contain credentials, query or fragment')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid REW API port') from exc
    if port is not None and not (1024 <= port <= 65535):
        raise ValueError('REW API port must be between 1024 and 65535')
    if parsed.path.rstrip('/'):
        raise ValueError('REW API URL must not contain a path')
    return f'http://{parsed.hostname}:{port or 4735}'


def decode_rew_float_array(encoded: str, *, max_samples: int = MAX_REW_ARRAY_SAMPLES) -> tuple[float, ...]:
    if max_samples < 0:
        raise ValueError('max_samples must be non-negative')
    if len(encoded) > max_base64_chars(4 * max_samples):
        raise RewApiResponseTooLarge(
            f'REW float array exceeds the {max_samples}-sample limit'
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RewApiError('Invalid Base64 array from REW') from exc
    if len(raw) % 4 != 0:
        raise RewApiError('REW float array byte length is not divisible by four')
    if len(raw) // 4 > max_samples:
        raise RewApiResponseTooLarge(
            f'REW float array exceeds the {max_samples}-sample limit'
        )
    if not raw:
        return ()
    values = tuple(struct.unpack(f'>{len(raw) // 4}f', raw))
    if any(not math.isfinite(value) for value in values):
        raise RewApiError('REW float array contains non-finite values')
    return values


def _field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


def decode_frequency_response(
    measurement_id: str,
    payload: dict[str, Any],
    *,
    requested_unit: str = 'SPL',
    requested_ppo: int | None = None,
    requested_smoothing: str | None = None,
) -> RewFrequencyResponse:
    magnitude_encoded = _field(payload, 'magnitude', 'magnitudes')
    if not isinstance(magnitude_encoded, str):
        raise RewApiError('REW frequency response does not contain magnitude data')
    magnitude = decode_rew_float_array(magnitude_encoded)
    if not magnitude:
        raise RewApiError('REW frequency response magnitude array is empty')

    start_frequency = _field(payload, 'startFreq', 'startFrequency')
    if not isinstance(start_frequency, (int, float)) or not math.isfinite(float(start_frequency)) or start_frequency <= 0:
        raise RewApiError('REW frequency response has an invalid start frequency')

    ppo_raw = _field(payload, 'ppo', 'pointsPerOctave')
    step_raw = _field(payload, 'freqStep', 'frequencyStep')
    ppo = float(ppo_raw) if isinstance(ppo_raw, (int, float)) and math.isfinite(float(ppo_raw)) and ppo_raw > 0 else None
    step = float(step_raw) if isinstance(step_raw, (int, float)) and math.isfinite(float(step_raw)) and step_raw > 0 else None
    if ppo is None and step is None:
        raise RewApiError('REW frequency response has neither ppo nor freqStep')

    try:
        if ppo is not None:
            frequency = tuple(float(start_frequency) * math.exp(index * math.log(2.0) / ppo) for index in range(len(magnitude)))
        else:
            assert step is not None
            frequency = tuple(float(start_frequency) + index * step for index in range(len(magnitude)))
    except OverflowError as exc:
        raise RewApiError('REW frequency axis overflowed the reported spacing') from exc
    if any(not math.isfinite(value) or value <= 0 for value in frequency):
        raise RewApiError('REW frequency axis is invalid')
    if any(
        frequency[index + 1] <= frequency[index]
        for index in range(len(frequency) - 1)
    ):
        raise RewApiError('REW frequency axis is not strictly increasing')

    phase_encoded = _field(payload, 'phase', 'phases')
    phase = decode_rew_float_array(phase_encoded) if isinstance(phase_encoded, str) and phase_encoded else None
    if phase is not None and len(phase) != len(magnitude):
        raise RewApiError('REW magnitude and phase array lengths differ')

    unit = payload.get('unit') if isinstance(payload.get('unit'), str) else None
    smoothing = payload.get('smoothing') if isinstance(payload.get('smoothing'), str) else None
    return RewFrequencyResponse(
        measurement_id=measurement_id,
        unit=unit,
        smoothing=smoothing,
        start_frequency_hz=float(start_frequency),
        points_per_octave=ppo,
        frequency_step_hz=step,
        frequency_hz=frequency,
        magnitude=magnitude,
        phase_deg=phase,
        requested_unit=requested_unit,
        requested_ppo=requested_ppo,
        requested_smoothing=requested_smoothing,
    )


def roomsim_position_to_htdt(room_depth_m: float, position: dict[str, Any]) -> dict[str, float]:
    if not math.isfinite(room_depth_m) or room_depth_m <= 0:
        raise ValueError('room depth must be positive')
    if position.get('unit') != 'metres':
        raise RewApiError('REW Room Simulator position must be requested in metres')
    values = {name: position.get(name) for name in ('fromRear', 'fromLeft', 'fromFloor')}
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values.values()):
        raise RewApiError('REW Room Simulator position is invalid')
    return {
        'x_m': float(values['fromLeft']),
        'y_m': room_depth_m - float(values['fromRear']),
        'z_m': float(values['fromFloor']),
    }


def htdt_position_to_roomsim(room_depth_m: float, position: dict[str, Any]) -> dict[str, float | str]:
    if not math.isfinite(room_depth_m) or room_depth_m <= 0:
        raise ValueError('room depth must be positive')
    values = {name: position.get(name) for name in ('x_m', 'y_m', 'z_m')}
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values.values()):
        raise ValueError('HTDT position is invalid')
    return {
        'unit': 'metres',
        'fromRear': room_depth_m - float(values['y_m']),
        'fromLeft': float(values['x_m']),
        'fromFloor': float(values['z_m']),
    }


def normalize_measurement_summaries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get('measurements'), list):
            return [item for item in payload['measurements'] if isinstance(item, dict)]
        values = [item for item in payload.values() if isinstance(item, dict)]
        if values and all('uuid' in item or 'title' in item for item in values):
            return values
        if payload == {}:
            return []
    raise RewApiError('Unexpected REW measurements response shape')


class RewApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_REW_API_URL,
        *,
        timeout_s: float = 1.5,
        opener: Callable[..., Any] = urlopen,
        max_response_bytes: int = MAX_REW_API_RESPONSE_BYTES,
    ) -> None:
        self.base_url = validate_rew_api_url(base_url)
        if max_response_bytes < 0:
            raise ValueError('max_response_bytes must be non-negative')
        self.timeout_s = timeout_s
        self._opener = opener
        self.max_response_bytes = max_response_bytes

    def _get_json(self, path: str, query: dict[str, str | int | float | bool] | None = None) -> Any:
        url = f'{self.base_url}{path}'
        if query:
            url = f'{url}?{urlencode(query)}'
        request = Request(url, headers={'Accept': 'application/json'}, method='GET')
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                raw = read_response_bounded(
                    response,
                    self.max_response_bytes,
                    label='REW API response',
                )
        except IngressTooLargeError as exc:
            raise RewApiResponseTooLarge(str(exc)) from exc
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RewApiUnavailable(str(exc)) from exc
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RewApiError('REW returned invalid JSON') from exc

    def list_measurements(self) -> list[dict[str, Any]]:
        return normalize_measurement_summaries(self._get_json('/measurements'))

    def get_audio_preflight(self) -> dict[str, Any]:
        audio_status = self._get_json('/audio/status')
        driver_payload = self._get_json('/audio/driver')
        sample_rate = self._get_json('/audio/samplerate')
        if not isinstance(audio_status, dict) or not isinstance(driver_payload, dict) or not isinstance(sample_rate, dict):
            raise RewApiError('Unexpected REW audio status response shape')

        driver = driver_payload.get('driver')
        if not isinstance(driver, str) or not driver:
            raise RewApiError('REW audio driver response is invalid')
        result: dict[str, Any] = {
            'read_only': True,
            'audio_enabled': audio_status.get('enabled') if isinstance(audio_status.get('enabled'), bool) else None,
            'audio_ready': audio_status.get('ready') if isinstance(audio_status.get('ready'), bool) else None,
            'driver': driver,
            'sample_rate_hz': float(sample_rate['value']) if isinstance(sample_rate.get('value'), (int, float)) else None,
            'sample_rate_unit': sample_rate.get('unit') if isinstance(sample_rate.get('unit'), str) else None,
            'java': None,
            'warnings': [],
        }
        if driver != 'Java':
            result['warnings'].append('Java multichannel preflight is not applicable while REW is using another driver.')
            return result

        input_device = self._get_json('/audio/java/input-device')
        input_devices = self._get_json('/audio/java/input-devices')
        input_hw_channels = self._get_json('/audio/java/num-input-device-channels')
        input_selection = self._get_json('/audio/java/input')
        input_choices = self._get_json('/audio/java/inputs')
        input_channel = self._get_json('/audio/java/input-channel')
        input_channels = self._get_json('/audio/java/num-input-channels')
        input_cal = self._get_json('/audio/input-cal')
        output_device = self._get_json('/audio/java/output-device')
        output_devices = self._get_json('/audio/java/output-devices')
        channel_count = self._get_json('/audio/java/num-output-device-channels')
        output_channels = self._get_json('/audio/java/output-channels')
        mapping = self._get_json('/audio/java/output-channel-mapping')
        stereo_only = self._get_json('/audio/java/stereo-only')
        if not isinstance(input_device, dict) or not isinstance(input_devices, list):
            raise RewApiError('Unexpected REW Java input-device response shape')
        if not isinstance(input_hw_channels, int) or input_hw_channels < 0 or not isinstance(input_selection, dict) or not isinstance(input_choices, list):
            raise RewApiError('Unexpected REW Java input response shape')
        if not isinstance(input_channel, dict) or not isinstance(input_channels, int) or input_channels < 0 or not isinstance(input_cal, dict):
            raise RewApiError('Unexpected REW Java input-channel response shape')
        if not isinstance(output_device, dict) or not isinstance(output_devices, list):
            raise RewApiError('Unexpected REW Java output-device response shape')
        if not isinstance(channel_count, int) or channel_count < 0 or not isinstance(output_channels, list):
            raise RewApiError('Unexpected REW Java output-channel response shape')
        if not isinstance(mapping, dict) or not isinstance(mapping.get('mapping'), list) or not isinstance(stereo_only, dict):
            raise RewApiError('Unexpected REW Java output mapping response shape')

        current_input = input_device.get('device') if isinstance(input_device.get('device'), str) else None
        current_input_selection = input_selection.get('input') if isinstance(input_selection.get('input'), str) else None
        selected_input_channel = input_channel.get('channel') if isinstance(input_channel.get('channel'), int) else None
        input_endpoint_ready = bool(result['audio_ready'] and current_input and input_channels > 0 and selected_input_channel is not None)
        current = output_device.get('device') if isinstance(output_device.get('device'), str) else None
        choices = [item for item in output_devices if isinstance(item, str)]
        channel_choices = [item for item in output_channels if isinstance(item, str)]
        exclusive = [item for item in choices if item.startswith('EXCL:')]
        stereo_flag = stereo_only.get('enable') if isinstance(stereo_only.get('enable'), bool) else None
        current_is_exclusive = bool(current and current.startswith('EXCL:'))
        multichannel_ready = bool(result['audio_ready'] and current_is_exclusive and channel_count > 2 and stereo_flag is not True)
        cal_all = input_cal.get('calDataAllInputs') if isinstance(input_cal.get('calDataAllInputs'), dict) else {}
        cal_path = cal_all.get('calFilePath') if isinstance(cal_all.get('calFilePath'), str) else None
        result['java'] = {
            'input_device': current_input,
            'input_devices': [item for item in input_devices if isinstance(item, str)],
            'hardware_input_channels': input_hw_channels,
            'input': current_input_selection,
            'inputs': [item for item in input_choices if isinstance(item, str)],
            'input_channel': selected_input_channel,
            'input_channels': input_channels,
            'input_endpoint_ready': input_endpoint_ready,
            'input_cal_selection': input_cal.get('currentInputSelection') if isinstance(input_cal.get('currentInputSelection'), str) else None,
            'input_cal_file': cal_path,
            'input_cal_file_present': bool(cal_path),
            'output_device': current,
            'output_devices': choices,
            'hardware_output_channels': channel_count,
            'output_channels': channel_choices,
            'output_channel_mapping': mapping['mapping'],
            'stereo_only': stereo_flag,
            'exclusive_output_candidates': exclusive,
            'current_output_is_exclusive': current_is_exclusive,
            'multichannel_ready': multichannel_ready,
        }
        if not input_endpoint_ready:
            result['warnings'].append('Current REW input endpoint is not ready for capture.')
        if not cal_path:
            result['warnings'].append('No REW input calibration file is selected; measurement microphone calibration is not verified.')
        if not exclusive:
            result['warnings'].append('No WASAPI-exclusive (EXCL:) output device is visible to REW.')
        elif not current_is_exclusive:
            result['warnings'].append('Current Java output is not a WASAPI-exclusive (EXCL:) device.')
        elif channel_count <= 2:
            result['warnings'].append('Current REW output exposes only mono/stereo hardware channels.')
        if stereo_flag is True:
            result['warnings'].append('REW Java driver is restricted to stereo-only mode.')
        return result


    def get_roomsim_snapshot(self) -> RewRoomSimSnapshot:
        version = self._get_json('/version')
        room_size = self._get_json('/roomsim/room-size', {'unit': 'metres'})
        sealed = self._get_json('/roomsim/room-is-sealed')
        absorptions = self._get_json('/roomsim/absorptions')
        options = self._get_json('/roomsim/options')
        head = self._get_json('/roomsim/head-position', {'unit': 'metres'})
        offsets = self._get_json('/roomsim/mic-posn-offsets')
        active_payload = self._get_json('/roomsim/sources')
        recognized_payload = self._get_json('/roomsim/source-names')
        mic_payload = self._get_json('/roomsim/mic-positions')
        if not isinstance(version, dict) or not isinstance(version.get('message'), str):
            raise RewApiError('Unexpected REW version response shape')
        if not isinstance(room_size, dict):
            raise RewApiError('Unexpected REW Room Simulator room-size response shape')
        for key in ('length', 'width', 'height'):
            value = room_size.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise RewApiError('REW Room Simulator room dimensions are invalid')
        if room_size.get('unit') != 'metres' or not isinstance(sealed, bool):
            raise RewApiError('Unexpected REW Room Simulator room response')
        if not isinstance(absorptions, dict) or not isinstance(options, dict) or not isinstance(head, dict) or not isinstance(offsets, dict):
            raise RewApiError('Unexpected REW Room Simulator configuration response shape')
        if not isinstance(active_payload, dict) or not isinstance(active_payload.get('sources'), list):
            raise RewApiError('Unexpected REW Room Simulator sources response shape')
        if not isinstance(recognized_payload, list) or not isinstance(mic_payload, list):
            raise RewApiError('Unexpected REW Room Simulator names response shape')
        active = tuple(item for item in active_payload['sources'] if isinstance(item, str))
        recognized = tuple(item for item in recognized_payload if isinstance(item, str))
        mic_positions = tuple(item for item in mic_payload if isinstance(item, str))
        if len(active) != len(active_payload['sources']) or len(recognized) != len(recognized_payload) or len(mic_positions) != len(mic_payload):
            raise RewApiError('REW Room Simulator name list contains invalid values')
        if any(source not in recognized for source in active):
            raise RewApiError('REW Room Simulator active source is not recognized')
        room_depth = float(room_size['length'])
        source_state: dict[str, dict[str, Any]] = {}
        for source in active:
            escaped = quote(source, safe='')
            position = self._get_json(f'/roomsim/{escaped}/position', {'unit': 'metres'})
            configuration = self._get_json(f'/roomsim/{escaped}/configuration')
            if not isinstance(position, dict) or not isinstance(configuration, dict):
                raise RewApiError('Unexpected REW Room Simulator source response shape')
            source_state[source] = {
                'position_rew': position,
                'position_htdt': roomsim_position_to_htdt(room_depth, position),
                'configuration': configuration,
            }
        return RewRoomSimSnapshot(
            rew_version=version['message'], room_size=room_size, room_is_sealed=sealed, absorptions=absorptions, options=options,
            head_position_rew=head, head_position_htdt=roomsim_position_to_htdt(room_depth, head),
            mic_position_offsets=offsets, active_sources=active, recognized_sources=recognized, mic_positions=mic_positions, sources=source_state,
        )


    def get_roomsim_frequency_response(
        self, *, mic_position: str = 'Main', source_name: str | None = None
    ) -> RewRoomSimFrequencyResponse:
        mic_payload = self._get_json('/roomsim/mic-positions')
        if not isinstance(mic_payload, list) or any(not isinstance(item, str) for item in mic_payload):
            raise RewApiError('Unexpected REW Room Simulator mic-position response shape')
        if mic_position not in mic_payload:
            raise RewApiNotFound(f'Unknown REW Room Simulator mic position: {mic_position}')
        if source_name is None:
            path = '/roomsim/frequency-response'
        else:
            source_payload = self._get_json('/roomsim/source-names')
            if not isinstance(source_payload, list) or any(not isinstance(item, str) for item in source_payload):
                raise RewApiError('Unexpected REW Room Simulator source-name response shape')
            if source_name not in source_payload:
                raise RewApiNotFound(f'Unknown REW Room Simulator source: {source_name}')
            path = f'/roomsim/{quote(source_name, safe="")}/frequency-response'
        payload = self._get_json(path, {'micposition': mic_position})
        if not isinstance(payload, dict):
            raise RewApiError('Unexpected REW Room Simulator frequency-response response shape')
        decoded = decode_frequency_response(
            f'roomsim:{source_name or "all"}:{mic_position}', payload,
            requested_unit='SPL', requested_ppo=None, requested_smoothing=None,
        )
        return RewRoomSimFrequencyResponse(
            source_name=source_name, mic_position=mic_position,
            message=payload.get('message') if isinstance(payload.get('message'), str) else None,
            unit=decoded.unit, smoothing=decoded.smoothing, start_frequency_hz=decoded.start_frequency_hz,
            points_per_octave=decoded.points_per_octave, frequency_step_hz=decoded.frequency_step_hz,
            frequency_hz=decoded.frequency_hz, magnitude=decoded.magnitude, phase_deg=decoded.phase_deg,
        )


    def get_measurement(self, measurement_id: str) -> dict[str, Any]:
        payload = self._get_json(f'/measurements/{quote(measurement_id, safe="")}')
        if not isinstance(payload, dict):
            raise RewApiError('Unexpected REW measurement response shape')
        return payload

    def get_frequency_response(
        self,
        measurement_id: str,
        *,
        ppo: int | None = 96,
        unit: str = 'SPL',
        smoothing: str | None = None,
    ) -> RewFrequencyResponse:
        if ppo is not None and not (1 <= ppo <= 384):
            raise ValueError('ppo must be between 1 and 384')
        query: dict[str, str | int] = {'unit': unit}
        if ppo is not None:
            query['ppo'] = ppo
        if smoothing:
            query['smoothing'] = smoothing
        payload = self._get_json(f'/measurements/{quote(measurement_id, safe="")}/frequency-response', query)
        if not isinstance(payload, dict):
            raise RewApiError('Unexpected REW frequency-response response shape')
        return decode_frequency_response(
            measurement_id,
            payload,
            requested_unit=unit,
            requested_ppo=ppo,
            requested_smoothing=smoothing,
        )

    def get_frequency_response_snapshot(
        self, measurement_uuid: str, *, ppo: int | None = None, unit: str = 'SPL', smoothing: str | None = None
    ) -> RewFrequencyResponseSnapshot:
        if ppo is not None and not (1 <= ppo <= 384):
            raise ValueError('ppo must be between 1 and 384')
        matches = [item for item in self.list_measurements() if item.get('uuid') == measurement_uuid]
        if len(matches) == 0:
            raise RewApiNotFound(f'REW measurement UUID not found: {measurement_uuid}')
        if len(matches) > 1:
            raise RewApiError('Selected REW measurement UUID is not unique in the current measurement list')
        before = self.get_measurement(measurement_uuid)
        if before.get('uuid') != measurement_uuid:
            raise RewApiError('REW measurement summary UUID does not match the requested UUID')
        query: dict[str, str | int] = {'unit': unit}
        if ppo is not None:
            query['ppo'] = ppo
        if smoothing:
            query['smoothing'] = smoothing
        payload = self._get_json(f'/measurements/{quote(measurement_uuid, safe="")}/frequency-response', query)
        if not isinstance(payload, dict):
            raise RewApiError('Unexpected REW frequency-response response shape')
        after = self.get_measurement(measurement_uuid)
        if before != after:
            raise RewApiError('REW measurement changed while the snapshot was being read')
        decoded = decode_frequency_response(
            measurement_uuid, payload, requested_unit=unit, requested_ppo=ppo, requested_smoothing=smoothing
        )
        return RewFrequencyResponseSnapshot(before, query, payload, decoded)

    def status(self) -> dict[str, Any]:
        try:
            session = self.engine_session()
        except RewApiError as exc:
            return {
                'connected': False,
                'read_only': True,
                'base_url': self.base_url,
                'measurement_count': None,
                'rew_version': None,
                'capabilities': None,
                'adapter_versions': None,
                'error': str(exc),
            }
        return {
            'connected': True,
            'read_only': True,
            'base_url': self.base_url,
            'measurement_count': session.capability_snapshot.get('measurement_count'),
            'rew_version': session.engine_version,
            'capabilities': session.capability_snapshot['capabilities'],
            'adapter_versions': {
                'rew_api_snapshot': session.adapter_version,
                'roomsim': ROOMSIM_ADAPTER_VERSION,
            },
            'error': None,
        }

    def engine_session(
        self,
        *,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
    ) -> RewEngineSession:
        """Open an acquisition-session authority bound to this REW endpoint.

        Probes ``/version`` and ``/measurements`` once, then seals the
        producer version and per-capability support into a
        ``RewEngineSession`` whose ``semantic_sha256`` covers every field.
        Imported measurements merge ``session.payload()`` into their
        provenance so the evidence always names the exact engine build that
        produced it — a version change lands as a *different* session seal,
        never a silent content change.
        """
        if adapter_version is None:
            from .cad_measurements import CAD_REW_API_ADAPTER_VERSION
            adapter_version = CAD_REW_API_ADAPTER_VERSION
        try:
            version = self._get_json('/version')
        except Exception:
            # The /version probe is optional metadata: an older REW build or
            # a client that cannot serve it degrades to engine_version=None
            # rather than failing the whole session contract.
            version = None
        measurements = self.list_measurements()
        engine_version = (
            version['message']
            if isinstance(version, dict) and isinstance(version.get('message'), str)
            else None
        )
        capability_snapshot: dict[str, Any] = {
            'capabilities': {
                'version': engine_version is not None,
                'list_measurements': True,
                'frequency_response': True,
            },
            'measurement_count': len(measurements),
        }
        observed_at = datetime.now(timezone.utc).isoformat()
        payload = {
            'engine_kind': 'rew',
            'engine_version': engine_version,
            'adapter_id': adapter_id or 'htdt-rew-api',
            'adapter_version': adapter_version,
            'endpoint': self.base_url,
            'capability_snapshot': capability_snapshot,
            'observed_at_utc': observed_at,
        }
        return RewEngineSession(
            engine_session_id=sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
            ).hexdigest()[:32],
            semantic_sha256=sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
            ).hexdigest(),
            **payload,
        )


    @staticmethod
    def roomsim_snapshot_payload(snapshot: RewRoomSimSnapshot) -> dict[str, Any]:
        return {
            'classification': 'rew_room_simulator_configuration',
            'read_only': True,
            'adapter_version': ROOMSIM_ADAPTER_VERSION,
            'model_contract': {
                'prediction_kind': 'predicted',
                'geometry_support': 'rectangular_room_only',
                'exact_non_rectangular_geometry': False,
                'reference_box_htdt': {
                    'width_m': float(snapshot.room_size['width']),
                    'depth_m': float(snapshot.room_size['length']),
                    'height_m': float(snapshot.room_size['height']),
                },
            },
            'coordinate_contract': {
                'htdt_origin': 'front-left-floor',
                'htdt_axes': {'x': 'right', 'y': 'rear', 'z': 'up'},
                'rew_axes': {'x': 'fromLeft', 'y': 'fromRear', 'z': 'fromFloor'},
                'mapping': 'x=fromLeft; y=room_length-fromRear; z=fromFloor',
            },
            **asdict(snapshot),
        }

    @staticmethod
    def roomsim_response_payload(response: RewRoomSimFrequencyResponse) -> dict[str, Any]:
        return {
            'classification': 'predicted_rew_room_simulator',
            'read_only': True,
            'adapter_version': ROOMSIM_ADAPTER_VERSION,
            'model_contract': {
                'prediction_kind': 'predicted',
                'geometry_support': 'rectangular_room_only',
                'exact_non_rectangular_geometry': False,
            },
            **asdict(response),
        }

    @staticmethod
    def response_payload(response: RewFrequencyResponse) -> dict[str, Any]:
        return asdict(response)
