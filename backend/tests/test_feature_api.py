import base64
import math
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def _rew_text() -> bytes:
    rows: list[str] = []
    k_min = math.ceil(96 * math.log2(20.0))
    k_max = math.floor(96 * math.log2(200.0))
    frequencies = [2 ** (k / 96) for k in range(k_min, k_max + 1)]
    dip_index = min(range(len(frequencies)), key=lambda i: abs(math.log2(frequencies[i] / 80.0)))
    for index, frequency in enumerate(frequencies):
        level = 61.0 if index == dip_index else 70.0
        rows.append(f'{frequency:.9f} {level:.3f}')
    return ('\n'.join(rows) + '\n').encode()


def _base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _context_payload() -> dict:
    return {
        'room': {'width_m': 343.0 / 160.0, 'depth_m': 4.0, 'height_m': 2.4},
        'speakers': [],
        'measurement_point': {
            'point_id': 'MLP',
            'label': 'MLP',
            'position': {'x_m': 1.0, 'y_m': 2.0, 'z_m': 1.0},
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def _import(client: TestClient, project_id: str, context_id: str, *, evidence_type: str, quality_status: str) -> dict:
    raw = _rew_text()
    response = client.post(f'/api/projects/{project_id}/measurements', json={
        'filename': 'synthetic.txt',
        'raw_base64': _base64(raw),
        'context_id': context_id,
        'channel_role': 'front_left',
        'evidence_type': evidence_type,
        'source_speaker_ids': ['FL'],
        'radiation_scope': 'single',
        'quality_status': quality_status,
        'quality_source': 'manual',
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_feature_candidate_api_matches_measured_unknown_quality_provisionally(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=_context_payload()).json()
    imported = _import(client, project['id'], context['id'], evidence_type='measured', quality_status='unknown')

    response = client.get(
        f"/api/projects/{project['id']}/datasets/{imported['dataset_id']}/feature-candidates"
        '?low_hz=30&high_hz=160&prominence_db=3'
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['classification'] == 'candidate_association_not_causal_diagnosis'
    assert payload['eligible_for_candidate_matching'] is True
    assert payload['feature_detection']['features']
    dip = next(feature for feature in payload['feature_detection']['features'] if feature['kind'] == 'dip')
    assert abs(math.log2(dip['frequency_hz'] / 80.0)) < 1 / 96
    assert any(match['candidate_kind'] == 'room_mode' and match['details']['n_x'] == 1 for match in payload['candidate_matches'])
    assert any('provisional' in warning for warning in payload['warnings'])


def test_feature_candidate_api_disables_matching_for_invalid_or_nonmeasured(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=_context_payload()).json()

    invalid = _import(client, project['id'], context['id'], evidence_type='measured', quality_status='invalid')
    invalid_payload = client.get(
        f"/api/projects/{project['id']}/datasets/{invalid['dataset_id']}/feature-candidates?low_hz=30&high_hz=160"
    ).json()
    assert invalid_payload['eligible_for_candidate_matching'] is False
    assert invalid_payload['candidate_matches'] == []

    predicted = _import(client, project['id'], context['id'], evidence_type='predicted', quality_status='usable')
    predicted_payload = client.get(
        f"/api/projects/{project['id']}/datasets/{predicted['dataset_id']}/feature-candidates?low_hz=30&high_hz=160"
    ).json()
    assert predicted_payload['eligible_for_candidate_matching'] is False
    assert predicted_payload['candidate_matches'] == []
