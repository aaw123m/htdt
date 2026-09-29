"""Round 14 — HTTP API surface truth.

Locks in the error-contract fixes this round made to the FastAPI layer:
uniform JSON ``{"detail": ...}`` bodies for every status (including an
uncaught 500), operator-facing 404 sentences instead of ``str(KeyError)``
reprs, 404 for upstream-not-found REW entities instead of a misleading 502,
and 422 for blank-after-strip project names.
"""

import json
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from htdt.database import Store
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


CONTEXT_BODY = {
    'room': {'width_m': 4.0, 'depth_m': 3.0, 'height_m': 2.5},
    'speakers': [
        {'speaker_id': 'fl', 'role': 'FL', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
    ],
    'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 2.0, 'z_m': 1.0}},
}


def make_client(
    tmp_path: Path, rew: RewApiClient | None = None, *, raise_server_exceptions: bool = True
) -> TestClient:
    return TestClient(create_app(tmp_path, rew_client=rew), raise_server_exceptions=raise_server_exceptions)


def seed_context(client: TestClient) -> tuple[str, str]:
    project = client.post('/api/projects', json={'name': 'demo'})
    assert project.status_code == 201
    project_id = project.json()['id']
    context = client.post(f'/api/projects/{project_id}/contexts', json=CONTEXT_BODY)
    assert context.status_code == 201
    return project_id, context.json()['id']


def assert_error_shape(response) -> None:
    """Every non-2xx body must stay one {"detail": ...} object the SPA parses."""
    assert response.headers['content-type'].startswith('application/json')
    body = response.json()
    assert isinstance(body, dict)
    assert 'detail' in body
    assert isinstance(body['detail'], (str, list))


def test_store_fault_returns_safe_json_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ServerErrorMiddleware sends the handler's response and then re-raises
    # for server logging, so the test client must not mirror that re-raise.
    client = make_client(tmp_path, raise_server_exceptions=False)
    project_id, _ = seed_context(client)

    def boom(self: Store, *args: object, **kwargs: object) -> list[dict]:
        raise RuntimeError('simulated store fault')

    monkeypatch.setattr(Store, 'list_contexts', boom)
    response = client.get(f'/api/projects/{project_id}/contexts')
    assert response.status_code == 500
    assert_error_shape(response)
    assert response.json()['detail'] == 'Internal Server Error'


def test_keyerror_404_uses_sentence_not_python_repr(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post('/api/projects/nope/sessions', json={'purpose': 'x'})
    assert response.status_code == 404
    assert_error_shape(response)
    assert response.json()['detail'] == 'Project not found'

    response = client.post('/api/projects/nope/contexts', json=CONTEXT_BODY)
    assert response.status_code == 404
    assert response.json()['detail'] == 'Project not found'

    project_id, _ = seed_context(client)
    response = client.post(
        f'/api/projects/{project_id}/contexts',
        json={**CONTEXT_BODY, 'parent_context_id': 'missing'},
    )
    assert response.status_code == 404
    assert response.json()['detail'] == 'Parent context not found'


def test_blank_project_name_is_422(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    for name in ('   ', '\t\n'):
        response = client.post('/api/projects', json={'name': name})
        assert response.status_code == 422
        assert_error_shape(response)


def test_valid_project_response_shape(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post('/api/projects', json={'name': 'demo'})
    assert response.status_code == 201
    body = response.json()
    assert isinstance(body['id'], str) and body['name'] == 'demo'


def test_wrong_method_is_405_with_detail(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post('/api/health')
    assert response.status_code == 405
    assert_error_shape(response)


def test_form_body_on_json_route_is_422(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post('/api/projects', content=b'name=x', headers={'content-type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 422
    assert_error_shape(response)


def test_traversal_id_is_plain_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get('/api/projects/..%2F..%2Fetc/contexts')
    assert response.status_code in (404, 422)
    assert_error_shape(response)


def test_unknown_rew_measurement_uuid_is_404(tmp_path: Path) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        if request.full_url.endswith('/measurements'):
            return FakeResponse({'1': {'uuid': 'known-uuid', 'title': 'FL', 'startFreq': 20, 'endFreq': 20000}})
        raise AssertionError(request.full_url)

    client = make_client(tmp_path, rew=RewApiClient(opener=opener))
    project_id, context_id = seed_context(client)
    response = client.post(
        f'/api/projects/{project_id}/rew-snapshots',
        json={'measurement_uuid': 'ghost-uuid', 'context_id': context_id, 'channel_role': 'FL'},
    )
    assert response.status_code == 404
    assert_error_shape(response)


def test_unknown_roomsim_mic_position_is_404(tmp_path: Path) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        if request.full_url.endswith('/roomsim/mic-positions'):
            return FakeResponse(['Main'])
        raise AssertionError(request.full_url)

    client = make_client(tmp_path, rew=RewApiClient(opener=opener))
    response = client.get('/api/rew/roomsim/frequency-response', params={'mic_position': 'Bogus'})
    assert response.status_code == 404
    assert_error_shape(response)


def test_unknown_roomsim_source_is_404(tmp_path: Path) -> None:
    def opener(request: Request, timeout: float) -> FakeResponse:
        if request.full_url.endswith('/roomsim/mic-positions'):
            return FakeResponse(['Main'])
        if request.full_url.endswith('/roomsim/source-names'):
            return FakeResponse(['FL'])
        raise AssertionError(request.full_url)

    client = make_client(tmp_path, rew=RewApiClient(opener=opener))
    response = client.get(
        '/api/rew/roomsim/frequency-response',
        params={'mic_position': 'Main', 'source': 'Bogus'},
    )
    assert response.status_code == 404
    assert_error_shape(response)
