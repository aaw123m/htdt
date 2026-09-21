from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def _context(*, fl_x: float, parent_context_id: str | None = None) -> dict:
    payload = {
        'room': {'width_m': 4.2, 'depth_m': 5.4, 'height_m': 2.4},
        'speakers': [
            {'speaker_id': 'FL', 'role': 'front_left', 'position': {'x_m': fl_x, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'C', 'role': 'front_center', 'position': {'x_m': 2.1, 'y_m': 0.8, 'z_m': 1.0}},
            {'speaker_id': 'FR', 'role': 'front_right', 'position': {'x_m': 3.2, 'y_m': 1.0, 'z_m': 1.0}},
            {'speaker_id': 'HL', 'role': 'height_front_left', 'position': {'x_m': 1.1, 'y_m': 1.2, 'z_m': 2.2}},
            {'speaker_id': 'HR', 'role': 'height_front_right', 'position': {'x_m': 3.1, 'y_m': 1.2, 'z_m': 2.2}},
        ],
        'measurement_point': {'point_id': 'MLP', 'label': 'MLP', 'position': {'x_m': 2.1, 'y_m': 3.5, 'z_m': 1.05}},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A', 'processing_mode': 'straight'},
    }
    if parent_context_id is not None:
        payload['parent_context_id'] = parent_context_id
    return payload


def _fr(levels: tuple[float, ...]) -> bytes:
    frequencies = (20, 40, 80, 160, 320)
    return ('\n'.join(f'{frequency} {level}' for frequency, level in zip(frequencies, levels, strict=True)) + '\n').encode()


def test_m10_synthetic_workflow_survives_restart(tmp_path: Path) -> None:
    data_root = tmp_path / 'primary'
    app = create_app(data_root)
    with TestClient(app) as client:
        project_response = client.post('/api/projects', json={'name': 'M10 synthetic theater'})
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()

        r1_response = client.post(f"/api/projects/{project['id']}/contexts", json=_context(fl_x=1.0))
        assert r1_response.status_code == 201, r1_response.text
        r1 = r1_response.json()
        assert r1['revision_number'] == 1

        def import_measurement(filename: str, raw: bytes, *, context_id: str, repeat_group: str) -> dict:
            response = client.post(f"/api/projects/{project['id']}/measurements", json={
                'filename': filename,
                'raw_base64': _b64(raw),
                'context_id': context_id,
                'channel_role': 'front_left',
                'evidence_type': 'measured',
                'source_speaker_ids': ['FL'],
                'radiation_scope': 'single',
                'routing_evidence': 'manual',
                'quality_status': 'usable',
                'quality_reasons': ['synthetic fixture accepted'],
                'quality_source': 'manual',
                'repeat_group': repeat_group,
            })
            assert response.status_code == 201, response.text
            return response.json()

        baseline_raw = _fr((70.0, 71.0, 73.0, 71.0, 70.0))
        baseline = import_measurement('r1-fl.txt', baseline_raw, context_id=r1['id'], repeat_group='r1-fl-repeat')
        repeat = import_measurement('r1-fl-repeat.txt', baseline_raw, context_id=r1['id'], repeat_group='r1-fl-repeat')
        assert repeat['duplicate_asset'] is True

        attachment_bytes = b'synthetic-mdat-session-for-m10'
        attachment_response = client.post(f"/api/projects/{project['id']}/attachments", json={
            'filename': 'm10-session.mdat',
            'raw_base64': _b64(attachment_bytes),
            'kind': 'mdat',
            'label': 'M10 synthetic source',
            'measurement_id': baseline['measurement_id'],
            'context_id': r1['id'],
        })
        assert attachment_response.status_code == 201, attachment_response.text
        attachment = attachment_response.json()

        r2_response = client.post(f"/api/projects/{project['id']}/contexts", json=_context(fl_x=1.15, parent_context_id=r1['id']))
        assert r2_response.status_code == 201, r2_response.text
        r2 = r2_response.json()
        assert r2['revision_number'] == 2
        assert r2['parent_context_id'] == r1['id']

        moved = import_measurement(
            'r2-fl.txt',
            _fr((70.0, 70.5, 71.5, 70.8, 70.0)),
            context_id=r2['id'],
            repeat_group='r2-fl',
        )

        comparison_response = client.post(f"/api/projects/{project['id']}/comparisons", json={
            'dataset_a_id': baseline['dataset_id'],
            'dataset_b_id': moved['dataset_id'],
            'low_hz': 40,
            'high_hz': 200,
            'reference_low_hz': 40,
            'reference_high_hz': 160,
            'expected_change_paths': ['speakers.FL.position'],
            'label': 'R1 to R2 FL move',
        })
        assert comparison_response.status_code == 201, comparison_response.text
        comparison = comparison_response.json()
        assert comparison['result']['algorithm_version'] == 'fr-compare-1'
        assert any(item['path'].startswith('speakers.FL.position') for item in comparison['result']['intended_changes'])
        assert comparison['result']['confounders'] == []

        r1_after = client.get(f"/api/projects/{project['id']}/contexts").json()
        r1_saved = next(item for item in r1_after if item['id'] == r1['id'])
        assert r1_saved['payload']['speakers'][0]['position']['x_m'] == 1.0

        report_response = client.get(f"/api/projects/{project['id']}/comparisons/{comparison['id']}/report.html")
        assert report_response.status_code == 200
        assert baseline['dataset_id'] in report_response.text
        assert moved['dataset_id'] in report_response.text
        assert '<svg' in report_response.text

    # Simulate application restart against the same data directory. The retired
    # browser backup/restore endpoints are covered by
    # test_browser_backup_retired.py; the supported .htdt-backup authority is
    # covered by test_native_backup.py.
    with TestClient(create_app(data_root)) as restarted:
        projects = restarted.get('/api/projects').json()
        assert [item['id'] for item in projects] == [project['id']]
        saved_comparisons = restarted.get(f"/api/projects/{project['id']}/comparisons").json()
        assert saved_comparisons[0]['id'] == comparison['id']
        assert saved_comparisons[0]['result']['shape_rms_db'] == comparison['result']['shape_rms_db']
        assert restarted.get('/api/integrity').json() == {'status': 'ok', 'problems': []}
        restarted_measurements = restarted.get(f"/api/projects/{project['id']}/measurements").json()
        assert {item['dataset_id'] for item in restarted_measurements} == {
            baseline['dataset_id'], repeat['dataset_id'], moved['dataset_id']
        }
        restarted_attachments = restarted.get(f"/api/projects/{project['id']}/attachments").json()
        restarted_attachment = next(item for item in restarted_attachments if item['id'] == attachment['id'])
        assert restarted_attachment['asset_sha256'] == attachment['asset_sha256']
        restarted_contexts = restarted.get(f"/api/projects/{project['id']}/contexts").json()
        assert {item['revision_number'] for item in restarted_contexts} == {1, 2}
