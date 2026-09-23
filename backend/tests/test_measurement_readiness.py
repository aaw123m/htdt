import base64
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from htdt.main import create_app
from htdt.readiness import evaluate_measurement_readiness


def context_payload(filename: str = '7000001_90deg.txt') -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0},
            'aim_xyz': [0.0, 0.0, 1.0],
        },
        'microphone': {
            'manufacturer': 'miniDSP', 'model': 'UMIK-1', 'serial': '7000001',
            'connection': 'usb', 'sample_rate_hz': 48000,
            'calibration_profile': '90deg', 'calibration_filename': filename,
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def rew_preflight(cal_path: str) -> dict:
    return {
        'read_only': True, 'audio_ready': True, 'driver': 'Java',
        'sample_rate_hz': 48000.0,
        'java': {
            'input_device': 'UMIK-1', 'input_endpoint_ready': True,
            'input_cal_file': cal_path,
            'multichannel_ready': True,
        },
    }


def test_machine_ready_still_requires_manual_confirmation() -> None:
    raw = b'calibration bytes'
    digest = hashlib.sha256(raw).hexdigest()
    result = evaluate_measurement_readiness(
        context_payload(),
        rew_preflight(r'C:\cal\7000001_90deg.txt'),
        [{'kind': 'microphone_calibration', 'context_id': 'ctx',
          'filename': '7000001_90deg.txt', 'asset_sha256': digest}],
        context_id='ctx',
        current_calibration_sha256=digest,
    )
    assert result['machine_ready'] is True
    assert result['status'] == 'manual_confirmation_required'
    assert result['failed_check_keys'] == []
    assert result['manual_confirmation_required']


def test_same_filename_with_unverified_bytes_is_blocked() -> None:
    digest = hashlib.sha256(b'raw asset').hexdigest()
    result = evaluate_measurement_readiness(
        context_payload(), rew_preflight(r'C:\cal\7000001_90deg.txt'),
        [{'kind': 'microphone_calibration', 'context_id': 'ctx',
          'filename': '7000001_90deg.txt', 'asset_sha256': digest}],
        context_id='ctx', current_calibration_sha256=hashlib.sha256(b'other').hexdigest(),
    )
    assert result['machine_ready'] is False
    assert 'calibration_raw_asset_matches_active_file' in result['failed_check_keys']


def test_orientation_mismatch_is_blocked() -> None:
    payload = context_payload()
    payload['measurement_point']['aim_xyz'] = [0.0, -1.0, 0.0]
    digest = hashlib.sha256(b'cal').hexdigest()
    result = evaluate_measurement_readiness(
        payload, rew_preflight(r'C:\cal\7000001_90deg.txt'),
        [{'kind': 'microphone_calibration', 'context_id': 'ctx',
          'filename': '7000001_90deg.txt', 'asset_sha256': digest}],
        context_id='ctx', current_calibration_sha256=digest,
    )
    assert 'context_orientation_matches_calibration_profile' in result['failed_check_keys']


class StaticRew:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get_audio_preflight(self) -> dict:
        return self.payload


def test_readiness_endpoint_hashes_active_calibration_file(tmp_path: Path) -> None:
    cal = tmp_path / '7000001_90deg.txt'
    cal.write_bytes(b'actual calibration bytes')
    client = TestClient(create_app(tmp_path / 'data', rew_client=StaticRew(rew_preflight(str(cal)))))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    encoded = base64.b64encode(cal.read_bytes()).decode()
    attached = client.post(f"/api/projects/{project['id']}/attachments", json={
        'filename': cal.name,
        'raw_base64': encoded,
        'kind': 'microphone_calibration',
        'context_id': context['id'],
    })
    assert attached.status_code == 201, attached.text

    response = client.get(
        f"/api/projects/{project['id']}/contexts/{context['id']}/measurement-readiness"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['machine_ready'] is True
    assert result['status'] == 'manual_confirmation_required'
    by_key = {item['key']: item for item in result['checks']}
    assert by_key['calibration_raw_asset_matches_active_file']['passed'] is True


def test_readiness_endpoint_without_calibration_attachment_is_blocked(tmp_path: Path) -> None:
    cal = tmp_path / '7000001_90deg.txt'
    cal.write_bytes(b'actual calibration bytes')
    client = TestClient(create_app(tmp_path / 'data', rew_client=StaticRew(rew_preflight(str(cal)))))
    project = client.post('/api/projects', json={'name': 'Room'}).json()
    context = client.post(f"/api/projects/{project['id']}/contexts", json=context_payload()).json()
    result = client.get(
        f"/api/projects/{project['id']}/contexts/{context['id']}/measurement-readiness"
    ).json()
    assert result['machine_ready'] is False
    assert 'calibration_raw_asset_attached' in result['failed_check_keys']


def test_readiness_blocks_calibration_profile_filename_mismatch() -> None:
    payload = context_payload()
    payload['microphone']['calibration_profile'] = '90deg'
    payload['microphone']['calibration_filename'] = '7001234.txt'
    result = evaluate_measurement_readiness(payload, rew_preflight(r'C:\\cal\\7001234.txt'), [{
        'kind': 'microphone_calibration', 'context_id': 'ctx-1', 'filename': '7001234.txt',
    }], context_id='ctx-1')
    assert result['status'] == 'blocked'
    assert 'context_calibration_filename_matches_profile' in result['failed_check_keys']
