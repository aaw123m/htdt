from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def _context(fl_x: float, volume_db: float) -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [{'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': fl_x, 'y_m': 1.0, 'z_m': 1.0}}],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A', 'volume_db': volume_db},
    }


def _raw(offset: float = 0.0) -> str:
    data = '\n'.join(f'{f} {level + offset}' for f, level in ((20, 70), (40, 71), (80, 72), (160, 73), (320, 74))) + '\n'
    return base64.b64encode(data.encode()).decode()


def test_comparison_records_quality_repeatability_and_confounders(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context_a = client.post(f"/api/projects/{project['id']}/contexts", json=_context(1.0, -30.0)).json()
    context_b = client.post(f"/api/projects/{project['id']}/contexts", json={**_context(1.1, -28.0), 'parent_context_id': context_a['id']}).json()

    def add(context_id: str, filename: str, offset: float, quality: str) -> dict:
        response = client.post(f"/api/projects/{project['id']}/measurements", json={
            'filename': filename, 'raw_base64': _raw(offset), 'context_id': context_id, 'channel_role': 'front_left',
            'evidence_type': 'measured', 'source_speaker_ids': ['FL'], 'radiation_scope': 'single',
            'quality_status': quality, 'quality_reasons': ['fixture'], 'quality_source': 'manual', 'repeat_group': 'fl-repeat',
        })
        assert response.status_code == 201, response.text
        return response.json()

    a = add(context_a['id'], 'a.txt', 0.0, 'usable')
    b = add(context_b['id'], 'b.txt', 1.0, 'warning')
    response = client.post(f"/api/projects/{project['id']}/comparisons", json={
        'dataset_a_id': a['dataset_id'], 'dataset_b_id': b['dataset_id'], 'low_hz': 40, 'high_hz': 160,
        'reference_low_hz': 40, 'reference_high_hz': 160, 'expected_change_paths': ['speakers.FL.position'],
    })
    assert response.status_code == 201, response.text
    result = response.json()['result']
    assert result['comparison_role'] == 'repeatability'
    assert result['measurement_b']['quality_status'] == 'warning'
    assert any(item['path'] == 'speakers.FL.position.x_m' for item in result['intended_changes'])
    assert any(item['path'] == 'avr.volume_db' for item in result['confounders'])
    assert result['interpretation_warnings']


def test_attachment_and_integrity_api(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    payload = base64.b64encode(b'synthetic mdat placeholder').decode()
    response = client.post(f"/api/projects/{project['id']}/attachments", json={
        'filename': 'session.mdat', 'raw_base64': payload, 'kind': 'mdat', 'label': 'Synthetic fixture',
    })
    assert response.status_code == 201, response.text
    assert response.json()['kind'] == 'mdat'
    assert client.get(f"/api/projects/{project['id']}/attachments").json()[0]['filename'] == 'session.mdat'
    assert client.get('/api/integrity').json() == {'status': 'ok', 'problems': []}
