import base64
import json
import struct
from urllib.error import URLError
from urllib.request import Request

import pytest

from htdt.rew_api import RewApiClient, RewApiError, decode_frequency_response, decode_rew_float_array, normalize_measurement_summaries, validate_rew_api_url


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.raw = json.dumps(payload).encode()

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amt: int = -1) -> bytes:
        return self.raw if amt is None or amt < 0 else self.raw[:amt]


def encode(values: list[float]) -> str:
    return base64.b64encode(struct.pack(f'>{len(values)}f', *values)).decode()


def test_official_big_endian_decode_example() -> None:
    values = decode_rew_float_array('PgAAAD6AAAA+wAAAPwAAAA==')
    assert values == pytest.approx((0.125, 0.25, 0.375, 0.5))


def test_frequency_response_log_axis_and_provenance() -> None:
    response = decode_frequency_response('abc', {
        'unit': 'SPL',
        'smoothing': '1/48',
        'startFreq': 20.0,
        'ppo': 1,
        'magnitude': encode([70.0, 71.0, 72.0, 73.0]),
        'phase': encode([0.0, 10.0, 20.0, 30.0]),
    }, requested_unit='SPL', requested_ppo=96, requested_smoothing='1/24')
    assert response.frequency_hz == pytest.approx((20.0, 40.0, 80.0, 160.0))
    assert response.magnitude == pytest.approx((70.0, 71.0, 72.0, 73.0))
    assert response.phase_deg == pytest.approx((0.0, 10.0, 20.0, 30.0))
    assert response.requested_ppo == 96
    assert response.requested_smoothing == '1/24'
    assert response.smoothing == '1/48'


def test_frequency_response_linear_axis() -> None:
    response = decode_frequency_response('abc', {
        'startFreq': 10.0,
        'freqStep': 2.5,
        'magnitude': encode([1.0, 2.0, 3.0]),
    })
    assert response.frequency_hz == pytest.approx((10.0, 12.5, 15.0))
    assert response.phase_deg is None


def test_measurement_shapes_are_normalized() -> None:
    assert normalize_measurement_summaries([{'uuid': 'a'}]) == [{'uuid': 'a'}]
    assert normalize_measurement_summaries({'1': {'uuid': 'a'}, '2': {'uuid': 'b'}}) == [{'uuid': 'a'}, {'uuid': 'b'}]
    assert normalize_measurement_summaries({'measurements': [{'uuid': 'a'}]}) == [{'uuid': 'a'}]
    assert normalize_measurement_summaries({}) == []


def test_remote_rew_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_rew_api_url('http://192.168.1.10:4735')
    with pytest.raises(ValueError):
        validate_rew_api_url('https://127.0.0.1:4735')


def test_client_uses_get_only_and_preserves_query_provenance() -> None:
    calls: list[tuple[str, str]] = []

    def opener(request: Request, timeout: float) -> FakeResponse:
        calls.append((request.full_url, request.get_method()))
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': {'uuid': 'abc', 'title': 'FL'}})
        if '/frequency-response?' in request.full_url:
            return FakeResponse({'unit': 'SPL', 'smoothing': '1/48', 'startFreq': 20.0, 'ppo': 1, 'magnitude': encode([70.0, 71.0])})
        raise AssertionError(request.full_url)

    client = RewApiClient(opener=opener)
    assert client.status()['measurement_count'] == 1
    response = client.get_frequency_response('abc', ppo=96, smoothing='1/24')
    assert response.magnitude == pytest.approx((70.0, 71.0))
    assert response.requested_ppo == 96
    assert response.requested_smoothing == '1/24'
    assert calls and all(method == 'GET' for _, method in calls)
    assert any('ppo=96' in url and 'smoothing=1%2F24' in url for url, _ in calls)


def test_status_falls_back_offline_without_raising() -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        raise URLError('not running')

    status = RewApiClient(opener=opener).status()
    assert status['connected'] is False
    assert status['read_only'] is True
    assert status['measurement_count'] is None


def test_invalid_arrays_are_rejected() -> None:
    bad = base64.b64encode(b'123').decode()
    with pytest.raises(RewApiError):
        decode_rew_float_array(bad)
    non_finite = base64.b64encode(struct.pack('>f', float('nan'))).decode()
    with pytest.raises(RewApiError):
        decode_rew_float_array(non_finite)


def test_real_rew_beta135_measurement_fixture_is_normalized() -> None:
    from pathlib import Path

    fixture = Path(__file__).parent / 'fixtures' / 'rew_5_40_beta135_measurements.json'
    payload = json.loads(fixture.read_text(encoding='utf-8'))
    measurements = normalize_measurement_summaries(payload)

    assert len(measurements) == 1
    summary = measurements[0]
    assert summary['title'] == 'HTDT synthetic real-API fixture'
    assert summary['rewVersion'] == 'V5.40 beta 135'
    assert summary['startFreq'] == pytest.approx(20.0)
    assert summary['endFreq'] == pytest.approx(20041.156)


def test_real_rew_beta135_frequency_response_fixture_decodes() -> None:
    from pathlib import Path

    fixture = Path(__file__).parent / 'fixtures' / 'rew_5_40_beta135_frequency_response.json'
    payload = json.loads(fixture.read_text(encoding='utf-8'))
    response = decode_frequency_response('beta135-fixture', payload, requested_unit='SPL', requested_ppo=96)

    assert len(response.magnitude) == 958
    assert response.points_per_octave == pytest.approx(96.0)
    assert response.smoothing == '1/48'
    assert response.unit == 'SPL'
    assert response.phase_deg is None
    assert response.frequency_hz[0] == pytest.approx(20.0)
    assert response.frequency_hz[-1] == pytest.approx(20041.155831556098)
    assert response.magnitude[0] == pytest.approx(73.80723571777344)


def test_audio_preflight_java_exclusive_multichannel() -> None:
    payloads = {
        '/audio/status': {'enabled': True, 'ready': True},
        '/audio/driver': {'driver': 'Java'},
        '/audio/samplerate': {'value': 48000.0, 'unit': 'Hz'},
        '/audio/java/input-device': {'device': 'UMIK-1'},
        '/audio/java/input-devices': ['UMIK-1'],
        '/audio/java/num-input-device-channels': 1,
        '/audio/java/input': {'input': 'Default Input'},
        '/audio/java/inputs': ['Default Input'],
        '/audio/java/input-channel': {'channel': 1},
        '/audio/java/num-input-channels': 1,
        '/audio/input-cal': {'currentInputSelection': 'UMIK-1 Default Input', 'calDataAllInputs': {'calFilePath': 'umik.cal'}},
        '/audio/java/output-device': {'device': 'EXCL: RX-A4A (AMD High Definition Audio Device)'},
        '/audio/java/output-devices': ['Default Device', 'EXCL: RX-A4A (AMD High Definition Audio Device)'],
        '/audio/java/num-output-device-channels': 8,
        '/audio/java/output-channels': ['L', 'R', 'C', 'LFE', 'SL', 'SR', 'SBL', 'SBR'],
        '/audio/java/output-channel-mapping': {'mapping': [{'index': 1, 'hardwareChannel': 1, 'channelLabel': 'L'}]},
        '/audio/java/stereo-only': {'enable': False},
    }
    calls: list[str] = []
    def opener(request: Request, timeout: float) -> FakeResponse:
        from urllib.parse import urlparse
        path = urlparse(request.full_url).path
        calls.append(path)
        return FakeResponse(payloads[path])
    preflight = RewApiClient(opener=opener).get_audio_preflight()
    assert preflight['driver'] == 'Java'
    assert preflight['sample_rate_hz'] == 48000.0
    assert preflight['java']['input_device'] == 'UMIK-1'
    assert preflight['java']['input_endpoint_ready'] is True
    assert preflight['java']['input_cal_file_present'] is True
    assert preflight['java']['hardware_output_channels'] == 8
    assert preflight['java']['current_output_is_exclusive'] is True
    assert preflight['java']['multichannel_ready'] is True
    assert preflight['warnings'] == []
    assert calls and all(path.startswith('/audio/') for path in calls)


def test_audio_preflight_non_java_skips_java_endpoints() -> None:
    payloads = {
        '/audio/status': {'enabled': True, 'ready': True},
        '/audio/driver': {'driver': 'ASIO'},
        '/audio/samplerate': {'value': 48000.0, 'unit': 'Hz'},
    }
    calls: list[str] = []
    def opener(request: Request, timeout: float) -> FakeResponse:
        from urllib.parse import urlparse
        path = urlparse(request.full_url).path
        calls.append(path)
        return FakeResponse(payloads[path])
    preflight = RewApiClient(opener=opener).get_audio_preflight()
    assert preflight['driver'] == 'ASIO'
    assert preflight['java'] is None
    assert any('not applicable' in warning for warning in preflight['warnings'])
    assert calls == ['/audio/status', '/audio/driver', '/audio/samplerate']


def test_snapshot_requires_exactly_one_uuid_and_all_requests_are_get() -> None:
    calls: list[tuple[str, str]] = []
    summary = {'uuid': 'abc', 'title': 'FL', 'rewVersion': 'V5.40 beta 135'}

    def opener(request: Request, timeout: float) -> FakeResponse:
        calls.append((request.full_url, request.get_method()))
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': summary})
        if request.full_url.endswith('/measurements/abc'):
            return FakeResponse(summary)
        if '/measurements/abc/frequency-response?' in request.full_url:
            return FakeResponse({'unit': 'SPL', 'smoothing': '1/48', 'startFreq': 20.0, 'ppo': 96, 'magnitude': encode([70.0, 71.0])})
        raise AssertionError(request.full_url)

    snapshot = RewApiClient(opener=opener).get_frequency_response_snapshot('abc', ppo=96, unit='SPL')
    assert snapshot.measurement_summary == summary
    assert snapshot.query == {'unit': 'SPL', 'ppo': 96}
    assert snapshot.decoded.magnitude == pytest.approx((70.0, 71.0))
    assert len(calls) == 4
    assert all(method == 'GET' for _, method in calls)


@pytest.mark.parametrize(
    ('listing', 'expected'),
    [({}, 'not found'), ({'1': {'uuid': 'abc'}, '2': {'uuid': 'abc'}}, 'not unique')],
)
def test_snapshot_rejects_missing_or_duplicate_uuid(listing: object, expected: str) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        if request.full_url.endswith('/measurements'):
            return FakeResponse(listing)
        raise AssertionError('detail endpoint must not be called')

    with pytest.raises(RewApiError, match=expected):
        RewApiClient(opener=opener).get_frequency_response_snapshot('abc')


def test_snapshot_rejects_measurement_changed_between_reads() -> None:
    detail_reads = 0

    def opener(request: Request, timeout: float) -> FakeResponse:
        nonlocal detail_reads
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': {'uuid': 'abc', 'title': 'FL'}})
        if request.full_url.endswith('/measurements/abc'):
            detail_reads += 1
            return FakeResponse({'uuid': 'abc', 'title': 'FL', 'notes': 'before' if detail_reads == 1 else 'after'})
        if '/measurements/abc/frequency-response?' in request.full_url:
            return FakeResponse({'startFreq': 20.0, 'ppo': 96, 'magnitude': encode([70.0, 71.0])})
        raise AssertionError(request.full_url)

    with pytest.raises(RewApiError, match='changed while'):
        RewApiClient(opener=opener).get_frequency_response_snapshot('abc', ppo=96)
