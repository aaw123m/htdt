from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from htdt.conditions import context_differences
from htdt.main import create_app
from htdt.models import ContextCreate


def context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0},
            'aim_xyz': [0.0, 0.0, 1.0],
        },
        'microphone': {
            'manufacturer': 'miniDSP', 'model': 'UMIK-1',
            'serial': None, 'connection': 'usb', 'sample_rate_hz': 48000,
            'calibration_profile': '90deg', 'calibration_filename': None,
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def test_context_persists_umik1_snapshot_and_aim(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    response = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload())
    assert response.status_code == 201, response.text
    payload = response.json()['payload']
    assert payload['microphone']['model'] == 'UMIK-1'
    assert payload['microphone']['sample_rate_hz'] == 48000
    assert payload['microphone']['calibration_profile'] == '90deg'
    assert payload['measurement_point']['aim_xyz'] == [0.0, 0.0, 1.0]


def test_umik1_rejects_non_48k_snapshot() -> None:
    payload = context_payload()
    payload['microphone']['sample_rate_hz'] = 44100
    with pytest.raises(ValidationError, match='48000'):
        ContextCreate.model_validate(payload)


def test_microphone_change_is_a_context_difference() -> None:
    left = context_payload()
    right = context_payload()
    right['microphone'] = {**right['microphone'], 'calibration_profile': '0deg'}
    right['measurement_point'] = {**right['measurement_point'], 'aim_xyz': [0.0, -1.0, 0.0]}
    differences = context_differences(left, right)
    paths = {item['path'] for item in differences}
    assert 'microphone.calibration_profile' in paths
    assert 'measurement_point.aim_xyz' in paths


def test_microphone_change_is_context_difference() -> None:
    left = context_payload()
    right = context_payload()
    right['microphone']['calibration_profile'] = '0deg'
    right['measurement_point']['aim_xyz'] = [0.0, -1.0, 0.0]
    differences = context_differences(left, right)
    paths = {item['path'] for item in differences}
    assert 'microphone.calibration_profile' in paths
    assert 'measurement_point.aim_xyz' in paths
