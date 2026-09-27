"""Round-4 API-surface review regression tests.

Covers the items implemented in this round (docs/reviews/round4-api.md):

- The SPA ``/{path:path}`` fallback must never answer an ``/api`` URL with
  ``index.html`` (200 text/html) — unknown API paths get a JSON 404 for every
  method, including the gated-off ``/api/docs``/``/api/openapi.json``.
- Real ``/api/*`` routes keep working unchanged beside the fallback guard.
- Project-scoped list endpoints return 404 (not ``[]``) for a missing
  project, and a ``context_id`` filter outside the project returns 404.
- ``create_constraint_set`` maps a store-level ``KeyError`` (entity deleted
  between check and insert) to 404 instead of leaking a 500.
- Request models reject non-finite floats (``Infinity``/``NaN`` literals,
  which ``json.loads`` accepts) at validation instead of storing poisoned
  payloads that later serialize as invalid JSON.
- A DB-referenced asset missing on disk is a server integrity fault (500),
  not a client error — ``AssetIntegrityError`` is not a ``ValueError``.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import htdt.main
from htdt.main import create_app


INDEX_HTML = '<html><body>HTDT-SPA-INDEX</body></html>'


@pytest.fixture
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fake ``frontend/dist`` so the SPA catchall is registered (same trick as
    ``test_frontend_containment.py`` — ``create_app`` locates ``dist`` relative
    to ``htdt/main.py``)."""
    repo_root = tmp_path / 'repo'
    package_dir = repo_root / 'backend' / 'src' / 'htdt'
    package_dir.mkdir(parents=True)
    (package_dir / 'main.py').write_text('# stub for frontend/dist discovery', encoding='utf-8')
    dist = repo_root / 'frontend' / 'dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text(INDEX_HTML, encoding='utf-8')
    monkeypatch.setattr(htdt.main, '__file__', str(package_dir / 'main.py'))
    return TestClient(create_app(tmp_path / 'data'))


def _context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [
            {'speaker_id': 'fl', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0}},
    }


def _make_project_and_context(client: TestClient) -> tuple[str, str]:
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=_context_payload()).json()
    return project['id'], context['id']


@pytest.mark.parametrize(
    'url',
    [
        '/api/does-not-exist',
        '/api/projects/typo/contextss',
        '/api',
        '/api/',
        '/API/DOES-NOT-EXIST',
        '/api/docs',
        '/api/openapi.json',
    ],
)
def test_unknown_api_get_returns_json_404_not_spa(spa_client: TestClient, url: str) -> None:
    response = spa_client.get(url)
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('application/json')
    assert response.json() == {'detail': 'Not Found'}
    assert INDEX_HTML not in response.text


@pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
def test_unknown_api_unsafe_methods_return_json_404(spa_client: TestClient, method: str) -> None:
    response = spa_client.request(method, '/api/does-not-exist', content=b'{}')
    assert response.status_code == 404
    assert response.json() == {'detail': 'Not Found'}


def test_spa_fallback_still_serves_client_routes(spa_client: TestClient) -> None:
    for route in ('/', '/some/client/route', '/apix/not-an-api'):
        response = spa_client.get(route)
        assert response.status_code == 200
        assert response.text == INDEX_HTML


def test_real_api_routes_are_not_shadowed(spa_client: TestClient) -> None:
    assert spa_client.get('/api/health').status_code == 200
    response = spa_client.post('/api/projects', json={'name': 'Room'})
    assert response.status_code == 201
    assert spa_client.get('/api/projects').status_code == 200


def test_options_on_unknown_api_path_keeps_router_default(spa_client: TestClient) -> None:
    # OPTIONS is deliberately not claimed by the fallback: it must not answer
    # 404 (or index.html) as if an endpoint existed.
    assert spa_client.options('/api/does-not-exist').status_code == 405


@pytest.mark.parametrize(
    'suffix',
    ['sessions', 'contexts', 'constraint-sets', 'search-specs', 'measurements', 'attachments', 'comparisons'],
)
def test_list_endpoints_404_for_missing_project(tmp_path: Path, suffix: str) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.get(f'/api/projects/no-such-project/{suffix}')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Project not found'


def test_list_endpoints_still_return_lists_for_real_project(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, context_id = _make_project_and_context(client)
    for suffix in ('sessions', 'contexts', 'constraint-sets', 'search-specs', 'measurements', 'attachments', 'comparisons'):
        response = client.get(f'/api/projects/{project_id}/{suffix}')
        assert response.status_code == 200, suffix
        assert isinstance(response.json(), list), suffix
    filtered = client.get(f'/api/projects/{project_id}/constraint-sets?context_id={context_id}')
    assert filtered.status_code == 200
    assert filtered.json() == []


def test_list_context_filter_404_for_foreign_context(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, _ = _make_project_and_context(client)
    other = client.post('/api/projects', json={'name': 'Other'}).json()
    foreign = client.post(f"/api/projects/{other['id']}/contexts", json=_context_payload()).json()
    for suffix in ('constraint-sets', 'search-specs'):
        response = client.get(f"/api/projects/{project_id}/{suffix}?context_id={foreign['id']}")
        assert response.status_code == 404, suffix


def test_constraint_set_store_keyerror_maps_to_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(tmp_path)
    client = TestClient(app)
    project_id, context_id = _make_project_and_context(client)

    def _gone(*args, **kwargs):
        raise KeyError('context_not_found')

    monkeypatch.setattr(app.state.store, 'create_constraint_set', _gone)
    response = client.post(
        f'/api/projects/{project_id}/constraint-sets',
        json={'context_id': context_id, 'name': 'set', 'constraints': [], 'entity_profiles': []},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    'mutate',
    [
        lambda payload: payload['room'].update(width_m=float('inf')),
        lambda payload: payload['room'].update(depth_m=float('nan')),
        lambda payload: payload['speakers'][0].update(aim_xyz=[float('nan'), 0.0, 1.0]),
        lambda payload: payload.update(avr={'extra': {'target_curve_db': float('inf')}}),
    ],
)
def test_context_create_rejects_non_finite_floats(tmp_path: Path, mutate) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    payload = _context_payload()
    mutate(payload)
    # httpx's ``json=`` refuses non-finite floats; json.loads on the server
    # accepts the Infinity/NaN literals, so send the body ourselves.
    response = client.post(
        f"/api/projects/{project['id']}/contexts",
        content=json.dumps(payload),
        headers={'Content-Type': 'application/json'},
    )
    assert response.status_code == 422


def test_comparison_rejects_non_finite_band(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_id, _ = _make_project_and_context(client)
    response = client.post(
        f'/api/projects/{project_id}/comparisons',
        content=json.dumps(
            {'dataset_a_id': 'a', 'dataset_b_id': 'b', 'low_hz': float('inf'), 'high_hz': 200.0}
        ),
        headers={'Content-Type': 'application/json'},
    )
    assert response.status_code == 422


def test_missing_asset_on_disk_is_a_server_error(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), raise_server_exceptions=False)
    project_id, context_id = _make_project_and_context(client)
    raw = b'20 70\n40 71\n80 72\n'
    payload = {
        'filename': 'a.txt',
        'raw_base64': base64.b64encode(raw).decode(),
        'context_id': context_id,
        'channel_role': 'front_left',
        'evidence_type': 'measured',
        'source_speaker_ids': [],
        'radiation_scope': 'single',
    }
    assert client.post(f'/api/projects/{project_id}/measurements', json=payload).status_code == 201
    assets = list((tmp_path / 'assets').glob('*'))
    assert len(assets) == 1
    assets[0].unlink()
    # Re-importing the same bytes hits the DB-references-missing-file path.
    response = client.post(f'/api/projects/{project_id}/measurements', json=payload)
    assert response.status_code == 500


def test_b64_body_over_limit_still_413(tmp_path: Path) -> None:
    """Boundary check unchanged: the route-level body ceiling still applies to
    real endpoints (boundary installed only on the served app, exercised here
    via ``install_local_request_boundary``)."""
    from htdt.security import install_local_request_boundary

    app = create_app(tmp_path)
    install_local_request_boundary(app, allow_testserver=True)
    client = TestClient(app)
    # MAX_SMALL_JSON_BODY_BYTES = 2 MiB; one byte over must trip the ceiling
    # before the route handler or parser runs.
    response = client.post('/api/projects', content=b'x' * (2 * 1024 * 1024 + 1))
    assert response.status_code == 413
