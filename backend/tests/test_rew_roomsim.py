import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from htdt.main import create_app
from htdt.rew_api import (
    RewApiClient,
    RewApiError,
    htdt_position_to_roomsim,
    roomsim_position_to_htdt,
)


FIXTURES = Path(__file__).parent / 'fixtures'
STATE_FIXTURE = FIXTURES / 'rew_5_40_beta135_roomsim_state.json'
FR_FIXTURE = FIXTURES / 'rew_5_40_beta135_roomsim_frequency_response.json'


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.raw = json.dumps(payload).encode('utf-8')

    def __enter__(self) -> 'FakeResponse':
        return self
    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amt: int = -1) -> bytes:
        return self.raw if amt is None or amt < 0 else self.raw[:amt]


def _fixtures() -> tuple[dict, dict]:
    state = json.loads(STATE_FIXTURE.read_text(encoding='utf-8'))
    frequency_response = json.loads(FR_FIXTURE.read_text(encoding='utf-8'))
    return state, frequency_response


def _roomsim_opener(calls: list[tuple[str, str]]):
    state, frequency_response = _fixtures()

    def opener(request: Request, timeout: float) -> FakeResponse:
        calls.append((request.full_url, request.get_method()))
        parsed = urlparse(request.full_url)
        path = parsed.path
        query = parse_qs(parsed.query)
        assert request.get_method() == 'GET'
        if path == '/version':
            return FakeResponse(state['version'])
        if path == '/roomsim/room-size':
            assert query.get('unit') == ['metres']
            return FakeResponse(state['room_size'])
        if path == '/roomsim/room-is-sealed':
            return FakeResponse(state['room_is_sealed'])
        if path == '/roomsim/absorptions':
            return FakeResponse(state['absorptions'])
        if path == '/roomsim/options':
            return FakeResponse(state['options'])
        if path == '/roomsim/head-position':
            assert query.get('unit') == ['metres']
            return FakeResponse(state['head_position'])
        if path == '/roomsim/mic-posn-offsets':
            return FakeResponse(state['mic_posn_offsets'])
        if path == '/roomsim/sources':
            return FakeResponse({'sources': state['sources']['active']})
        if path == '/roomsim/source-names':
            return FakeResponse(state['sources']['recognized'])
        if path == '/roomsim/mic-positions':
            return FakeResponse(state['mic_positions'])
        if path == '/roomsim/frequency-response':
            assert query.get('micposition') == ['Main']
            return FakeResponse(frequency_response)
        if path.startswith('/roomsim/') and path.endswith('/frequency-response'):
            assert query.get('micposition') == ['Main']
            return FakeResponse(frequency_response)
        if path.startswith('/roomsim/') and path.endswith('/position'):
            source = unquote(path.split('/')[2])
            assert query.get('unit') == ['metres']
            return FakeResponse(state['sources']['details'][source]['position'])
        if path.startswith('/roomsim/') and path.endswith('/configuration'):
            source = unquote(path.split('/')[2])
            return FakeResponse(state['sources']['details'][source]['configuration'])
        raise AssertionError(request.full_url)

    return opener
def test_roomsim_coordinate_transform_roundtrip() -> None:
    rew = {'unit': 'metres', 'fromRear': 4.0, 'fromLeft': 1.0, 'fromFloor': 1.0}
    htdt = roomsim_position_to_htdt(5.0, rew)
    assert htdt == {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}
    assert htdt_position_to_roomsim(5.0, htdt) == rew


def test_roomsim_snapshot_is_get_only_and_converts_positions() -> None:
    calls: list[tuple[str, str]] = []
    snapshot = RewApiClient(opener=_roomsim_opener(calls)).get_roomsim_snapshot()
    assert snapshot.rew_version == '5.40 Beta 135 API 0.9.8'
    assert snapshot.room_size == {'unit': 'metres', 'length': 5.0, 'width': 4.0, 'height': 2.4}
    assert snapshot.head_position_htdt == {'x_m': 2.0, 'y_m': 3.1, 'z_m': 1.0}
    assert snapshot.sources['Left']['position_htdt'] == {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}
    assert snapshot.active_sources == ('Sub1', 'Left', 'Right')
    assert snapshot.mic_positions[0] == 'Main'
    assert calls and all(method == 'GET' for _, method in calls)
def test_real_beta135_roomsim_frequency_response_fixture_decodes() -> None:
    calls: list[tuple[str, str]] = []
    response = RewApiClient(opener=_roomsim_opener(calls)).get_roomsim_frequency_response()
    assert response.source_name is None
    assert response.mic_position == 'Main'
    assert response.message == 'All sources at Main'
    assert response.unit == 'SPL'
    assert response.smoothing == 'None'
    assert response.points_per_octave == pytest.approx(192.0)
    assert response.phase_deg is not None
    assert len(response.phase_deg) == 751
    assert len(response.frequency_hz) == 751
    assert response.frequency_hz[0] == pytest.approx(20.0)
    assert 290.0 < response.frequency_hz[-1] < 310.0
    assert response.magnitude[0] == pytest.approx(77.3044662475586)
    assert response.magnitude[-1] == pytest.approx(90.60202026367188)
    assert calls and all(method == 'GET' for _, method in calls)


def test_roomsim_unknown_mic_and_source_are_rejected_before_response_get() -> None:
    calls: list[tuple[str, str]] = []
    client = RewApiClient(opener=_roomsim_opener(calls))
    with pytest.raises(RewApiError, match='mic position'):
        client.get_roomsim_frequency_response(mic_position='Unknown')
    assert not any('/frequency-response' in url for url, _ in calls)
    calls.clear()
    with pytest.raises(RewApiError, match='source'):
        client.get_roomsim_frequency_response(source_name='Unknown')
    assert not any('/frequency-response' in url for url, _ in calls)


def test_roomsim_api_exposes_prediction_classification(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    rew = RewApiClient(opener=_roomsim_opener(calls))
    client = TestClient(create_app(tmp_path, rew_client=rew))

    state = client.get('/api/rew/roomsim/state')
    assert state.status_code == 200, state.text
    state_payload = state.json()
    assert state_payload['classification'] == 'rew_room_simulator_configuration'
    assert state_payload['read_only'] is True
    assert state_payload['adapter_version'] == 'rew-roomsim-readonly-1'
    assert state_payload['model_contract']['geometry_support'] == 'rectangular_room_only'
    assert state_payload['model_contract']['exact_non_rectangular_geometry'] is False
    assert state_payload['model_contract']['reference_box_htdt'] == {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4}
    assert state_payload['coordinate_contract']['htdt_origin'] == 'front-left-floor'
    assert state_payload['head_position_htdt']['y_m'] == pytest.approx(3.1)

    response = client.get('/api/rew/roomsim/frequency-response?mic_position=Main')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['classification'] == 'predicted_rew_room_simulator'
    assert payload['read_only'] is True
    assert payload['model_contract']['geometry_support'] == 'rectangular_room_only'
    assert payload['model_contract']['exact_non_rectangular_geometry'] is False
    assert payload['source_name'] is None
    assert payload['mic_position'] == 'Main'
    assert len(payload['frequency_hz']) == 751
    assert calls and all(method == 'GET' for _, method in calls)
