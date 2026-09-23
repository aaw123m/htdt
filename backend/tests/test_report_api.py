import base64
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_comparison_report_json_and_html_downloads(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Report room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json={
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [{'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0}}],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }).json()

    def import_fr(name: str, levels: list[float], quality: str) -> dict:
        rows = '\n'.join(f'{frequency} {level}' for frequency, level in zip((20, 40, 80, 160, 320), levels, strict=True)) + '\n'
        response = client.post(f"/api/projects/{project['id']}/measurements", json={
            'filename': name,
            'raw_base64': _b64(rows),
            'context_id': context['id'],
            'channel_role': 'front_left',
            'evidence_type': 'measured',
            'source_speaker_ids': ['FL'],
            'radiation_scope': 'single',
            'quality_status': quality,
            'quality_source': 'manual',
        })
        assert response.status_code == 201, response.text
        return response.json()

    a = import_fr('a.txt', [70, 71, 72, 71, 70], 'usable')
    b = import_fr('b.txt', [69, 70, 70, 70, 69], 'warning')
    comparison_response = client.post(f"/api/projects/{project['id']}/comparisons", json={
        'dataset_a_id': a['dataset_id'],
        'dataset_b_id': b['dataset_id'],
        'low_hz': 40,
        'high_hz': 200,
        'reference_low_hz': 40,
        'reference_high_hz': 160,
        'expected_change_paths': [],
        'label': 'Report test',
    })
    assert comparison_response.status_code == 201, comparison_response.text
    comparison = comparison_response.json()

    json_response = client.get(f"/api/projects/{project['id']}/comparisons/{comparison['id']}/report.json")
    assert json_response.status_code == 200
    assert 'attachment;' in json_response.headers['content-disposition']
    payload = json_response.json()
    assert payload['project']['name'] == 'Report room'
    assert payload['comparison']['id'] == comparison['id']
    assert payload['comparison']['result']['algorithm_version'] == 'fr-compare-1'

    html_response = client.get(f"/api/projects/{project['id']}/comparisons/{comparison['id']}/report.html")
    assert html_response.status_code == 200
    assert 'attachment;' in html_response.headers['content-disposition']
    assert '<svg' in html_response.text
    assert a['dataset_id'] in html_response.text
    assert 'B has measurement quality warnings' in html_response.text
    assert 'application/json' in html_response.text

    missing = client.get(f"/api/projects/{project['id']}/comparisons/not-found/report.html")
    assert missing.status_code == 404
