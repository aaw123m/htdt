from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any


def _check(key: str, passed: bool, detail: str) -> dict[str, Any]:
    return {'key': key, 'passed': passed, 'detail': detail}


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    return PureWindowsPath(value.replace('/', '\\')).name


def _is_umik1(value: str | None) -> bool:
    if not value:
        return False
    normalized = ''.join(ch for ch in value.upper() if ch.isalnum())
    return 'UMIK1' in normalized


def evaluate_measurement_readiness(
    context_payload: dict[str, Any],
    rew_preflight: dict[str, Any],
    attachments: list[dict[str, Any]],
    *,
    context_id: str,
    current_calibration_sha256: str | None = None,
) -> dict[str, Any]:
    microphone = context_payload.get('microphone')
    point = context_payload.get('measurement_point') if isinstance(context_payload.get('measurement_point'), dict) else {}
    java = rew_preflight.get('java') if isinstance(rew_preflight.get('java'), dict) else None
    checks: list[dict[str, Any]] = []

    mic_present = isinstance(microphone, dict)
    checks.append(_check('context_microphone_present', mic_present, 'Context includes a microphone snapshot' if mic_present else 'Context has no microphone snapshot'))
    mic = microphone if isinstance(microphone, dict) else {}
    model = mic.get('model') if isinstance(mic.get('model'), str) else None
    checks.append(_check('context_microphone_is_umik1', _is_umik1(model), f'Context microphone model: {model or "unknown"}'))
    serial = mic.get('serial') if isinstance(mic.get('serial'), str) and mic.get('serial').strip() else None
    checks.append(_check('context_umik1_serial_recorded', bool(serial), 'UMIK-1 serial is recorded' if serial else 'UMIK-1 serial is not recorded'))

    profile = mic.get('calibration_profile') if isinstance(mic.get('calibration_profile'), str) else 'unknown'
    aim = point.get('aim_xyz')
    expected_aim = [0.0, 0.0, 1.0] if profile == '90deg' else [0.0, -1.0, 0.0] if profile == '0deg' else None
    try:
        aim_matches = expected_aim is not None and isinstance(aim, (list, tuple)) and len(aim) == 3 and all(abs(float(a) - b) < 1e-9 for a, b in zip(aim, expected_aim))
    except (TypeError, ValueError):
        aim_matches = False
    checks.append(_check('context_orientation_matches_calibration_profile', aim_matches, f'calibration={profile}, aim={aim}'))

    checks.append(_check('rew_audio_ready', rew_preflight.get('audio_ready') is True, f'REW audio ready={rew_preflight.get("audio_ready")}'))
    checks.append(_check('rew_java_driver', rew_preflight.get('driver') == 'Java', f'REW driver={rew_preflight.get("driver") or "unknown"}'))
    input_device = java.get('input_device') if java else None
    checks.append(_check('rew_input_endpoint_ready', bool(java and java.get('input_endpoint_ready')), f'REW input endpoint ready={java.get("input_endpoint_ready") if java else None}'))
    checks.append(_check('rew_input_is_umik1', _is_umik1(input_device if isinstance(input_device, str) else None), f'REW input device: {input_device or "unknown"}'))

    expected_rate = mic.get('sample_rate_hz') if isinstance(mic.get('sample_rate_hz'), (int, float)) else None
    actual_rate = rew_preflight.get('sample_rate_hz') if isinstance(rew_preflight.get('sample_rate_hz'), (int, float)) else None
    rate_matches = expected_rate is not None and actual_rate is not None and abs(float(expected_rate) - float(actual_rate)) < 0.5
    checks.append(_check('rew_sample_rate_matches_context', rate_matches, f'Context={expected_rate or "unknown"} Hz, REW={actual_rate or "unknown"} Hz'))

    expected_cal = _basename(mic.get('calibration_filename') if isinstance(mic.get('calibration_filename'), str) else None)
    actual_cal = _basename(java.get('input_cal_file') if java and isinstance(java.get('input_cal_file'), str) else None)
    cal_selected = bool(actual_cal)
    checks.append(_check('rew_calibration_file_selected', cal_selected, f'REW calibration file: {actual_cal or "not selected"}'))
    cal_matches = bool(expected_cal and actual_cal and expected_cal.casefold() == actual_cal.casefold())
    checks.append(_check('rew_calibration_matches_context', cal_matches, f'Context={expected_cal or "not recorded"}, REW={actual_cal or "not selected"}'))
    expected_lower = expected_cal.casefold() if expected_cal else ''
    profile_filename_matches = bool(expected_cal) and ((profile == '90deg' and '_90deg' in expected_lower) or (profile == '0deg' and '_90deg' not in expected_lower))
    checks.append(_check('context_calibration_filename_matches_profile', profile_filename_matches, f'profile={profile}, filename={expected_cal or "not recorded"}'))

    attachment_matches = any(
        item.get('kind') == 'microphone_calibration'
        and item.get('context_id') == context_id
        and isinstance(item.get('filename'), str)
        and expected_cal is not None
        and _basename(item['filename']).casefold() == expected_cal.casefold()
        for item in attachments
    )
    checks.append(_check('calibration_raw_asset_attached', attachment_matches, 'Matching microphone calibration RawAsset is attached to this Context' if attachment_matches else 'Matching microphone calibration RawAsset is not attached to this Context'))
    hash_matches = bool(current_calibration_sha256 and any(
        item.get('kind') == 'microphone_calibration'
        and item.get('context_id') == context_id
        and item.get('asset_sha256') == current_calibration_sha256
        for item in attachments
    ))
    checks.append(_check(
        'calibration_raw_asset_matches_active_file',
        hash_matches,
        'Active REW calibration file bytes match a calibration RawAsset attached to this Context'
        if hash_matches else 'Active REW calibration file bytes are not verified against this Context RawAsset',
    ))
    multichannel_ready = bool(java and java.get('multichannel_ready'))
    checks.append(_check('rew_multichannel_output_ready', multichannel_ready, f'REW multichannel ready={java.get("multichannel_ready") if java else None}'))

    failed = [item for item in checks if not item['passed']]
    manual_checks = [
        'Confirm the physical UMIK-1 capsule is at the saved measurement-point coordinates.',
        'Confirm the physical UMIK-1 orientation matches the saved 0°/90° Context orientation.',
        'Confirm RX-A4A input, volume, processing, PEQ/YPAO and bass-management state match the saved AVR snapshot.',
        'Verify the selected REW output channel actually excites the intended physical speaker before marking routing evidence verified.',
    ]
    return {
        'classification': 'measurement_readiness_preflight_not_measurement_quality',
        'machine_ready': not failed,
        'status': 'manual_confirmation_required' if not failed else 'blocked',
        'context_id': context_id,
        'checks': checks,
        'failed_check_keys': [item['key'] for item in failed],
        'manual_confirmation_required': manual_checks,
        'notice': 'Passing this preflight does not prove microphone orientation, speaker routing, quiet-room conditions, clipping absence, or measurement quality.',
    }
