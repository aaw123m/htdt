import base64
import json
from pathlib import Path
import struct
from urllib.error import URLError
from urllib.request import Request

from fastapi.testclient import TestClient

from htdt.main import create_app
from htdt.rew_api import RewApiClient


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


def test_rew_read_only_endpoints(tmp_path: Path) -> None:
    methods: list[str] = []

    def opener(request: Request, timeout: float) -> FakeResponse:
        methods.append(request.get_method())
        if request.full_url.endswith('/audio/status'):
            return FakeResponse({'enabled': True, 'ready': True})
        if request.full_url.endswith('/audio/driver'):
            return FakeResponse({'driver': 'ASIO'})
        if request.full_url.endswith('/audio/samplerate'):
            return FakeResponse({'value': 48000.0, 'unit': 'Hz'})
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': {'uuid': 'abc', 'title': 'FL', 'startFreq': 20, 'endFreq': 20000}})
        if '/measurements/abc/frequency-response?' in request.full_url:
            return FakeResponse({'unit': 'SPL', 'smoothing': '1/48', 'startFreq': 20.0, 'ppo': 1, 'magnitude': encode([70.0, 71.0, 72.0])})
        raise AssertionError(request.full_url)

    rew = RewApiClient(opener=opener)
    client = TestClient(create_app(tmp_path, rew_client=rew))

    status = client.get('/api/rew/status')
    assert status.status_code == 200
    assert status.json()['connected'] is True
    assert status.json()['read_only'] is True

    preflight = client.get('/api/rew/audio-preflight')
    assert preflight.status_code == 200
    assert preflight.json()['driver'] == 'ASIO'
    assert preflight.json()['read_only'] is True

    measurements = client.get('/api/rew/measurements')
    assert measurements.status_code == 200
    assert measurements.json()[0]['uuid'] == 'abc'

    response = client.get('/api/rew/measurements/abc/frequency-response?ppo=96&smoothing=1%2F24')
    assert response.status_code == 200
    payload = response.json()
    assert payload['magnitude'] == [70.0, 71.0, 72.0]
    assert payload['requested_ppo'] == 96
    assert payload['requested_smoothing'] == '1/24'
    assert payload['smoothing'] == '1/48'
    assert methods and set(methods) == {'GET'}


def test_rew_status_is_offline_safe_and_data_endpoint_is_503(tmp_path: Path) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        raise URLError('REW not running')

    client = TestClient(create_app(tmp_path, rew_client=RewApiClient(opener=opener)))
    status = client.get('/api/rew/status')
    assert status.status_code == 200
    assert status.json()['connected'] is False
    assert status.json()['read_only'] is True
    assert client.get('/api/rew/measurements').status_code == 503
