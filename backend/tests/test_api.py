import base64
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def test_end_to_end_project_context_import_compare(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context_payload = {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [
            {'speaker_id': 'fl', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'fr', 'role': 'front_right', 'position': {'x_m': 3.0, 'y_m': 1.0, 'z_m': 1.0}},
        ],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.5, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload).json()

    dataset_ids = []
    for name, offset in [('a.txt', 0), ('b.txt', -3)]:
        raw = f'20 {70 + offset}\n40 {71 + offset}\n80 {72 + offset}\n160 {73 + offset}\n320 {74 + offset}\n'.encode()
        response = client.post(f"/api/projects/{project['id']}/measurements", json={'filename': name, 'raw_base64': base64.b64encode(raw).decode(), 'context_id': context['id'], 'channel_role': 'front_left', 'evidence_type': 'measured', 'source_speaker_ids': ['fl'], 'radiation_scope': 'single'})
        assert response.status_code == 201, response.text
        dataset_ids.append(response.json()['dataset_id'])

    comparison = client.post(f"/api/projects/{project['id']}/comparisons", json={'dataset_a_id': dataset_ids[0], 'dataset_b_id': dataset_ids[1], 'low_hz': 30, 'high_hz': 200, 'reference_low_hz': 40, 'reference_high_hz': 160})
    assert comparison.status_code == 201, comparison.text
    assert abs(comparison.json()['result']['mean_difference_db'] - 3.0) < 1e-10
