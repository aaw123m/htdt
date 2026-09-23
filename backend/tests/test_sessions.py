from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [{'speaker_id': 'fl', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}}],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def measurement_payload(context_id: str, session_id: str | None) -> dict:
    raw = b'20 70\n40 71\n80 72\n160 73\n320 74\n'
    return {
        'filename': 'fl.txt',
        'raw_base64': base64.b64encode(raw).decode(),
        'context_id': context_id,
        'session_id': session_id,
        'channel_role': 'front_left',
        'evidence_type': 'measured',
        'source_speaker_ids': ['fl'],
        'radiation_scope': 'single',
    }


def test_session_create_list_and_measurement_membership(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()

    session_response = client.post(
        f"/api/projects/{project['id']}/sessions",
        json={'purpose': 'Baseline repeatability', 'started_at': '2026-09-16T10:00:00+09:00', 'notes': 'MLP unchanged'},
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()

    imported = client.post(
        f"/api/projects/{project['id']}/measurements",
        json=measurement_payload(context['id'], session['id']),
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()['session_id'] == session['id']

    measurements = client.get(f"/api/projects/{project['id']}/measurements").json()
    assert measurements[0]['session_id'] == session['id']

    sessions = client.get(f"/api/projects/{project['id']}/sessions").json()
    assert sessions == [{**session, 'measurement_count': 1}]


def test_session_from_another_project_cannot_be_assigned(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project_a = client.post('/api/projects', json={'name': 'A'}).json()
    project_b = client.post('/api/projects', json={'name': 'B'}).json()
    session = client.post(f"/api/projects/{project_a['id']}/sessions", json={'purpose': 'A session'}).json()
    context = client.post(f"/api/projects/{project_b['id']}/contexts", json=context_payload()).json()

    response = client.post(
        f"/api/projects/{project_b['id']}/measurements",
        json=measurement_payload(context['id'], session['id']),
    )
    assert response.status_code == 404
    assert 'session_not_found' in response.text


def test_measurement_without_session_remains_explicitly_unassigned(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()

    response = client.post(
        f"/api/projects/{project['id']}/measurements",
        json=measurement_payload(context['id'], None),
    )
    assert response.status_code == 201, response.text
    assert response.json()['session_id'] is None
    assert client.get(f"/api/projects/{project['id']}/measurements").json()[0]['session_id'] is None
