from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def test_acoustics_endpoint_uses_saved_context(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json={
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [{'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}}],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }).json()
    response = client.get(f"/api/projects/{project['id']}/contexts/{context['id']}/acoustics?max_hz=120")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['classification'] == 'predicted_geometry_candidate'
    assert payload['room_modes']
    assert len(payload['first_order_reflections']) == 6
