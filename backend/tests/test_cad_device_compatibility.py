from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationExportSnapshot,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_device_adapter import build_device_binding
from htdt.cad_device_adapter_file import (
    FILE_ADAPTER_ID,
    FILE_ADAPTER_VERSION,
    FILE_READBACK_NAME,
    FileCalibrationAdapter,
)
from htdt.cad_device_compatibility import (
    ConformanceCase,
    DeviceCompatibilityMatrix,
    DeviceCompatibilityRow,
    DeviceQualificationRecord,
    resolve_firmware_capability,
    run_conformance_harness,
)

NOW = '2026-09-24T00:00:00+00:00'


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
    )


def _export() -> CadCalibrationExportSnapshot:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='out-1',
        sample_rate_hz=48000,
        gain_db=1.0,
        delay_s=0.001,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=('avr-ch1',),
    )
    payload = {
        'plan_id': 'plan-1',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': 'doc-1',
        'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000,
        'channels': (channel,),
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': _constraints(),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    plan = CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': hashlib.sha256(
                json.dumps(
                    provisional.semantic_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )
    return build_generic_biquad_export(plan=plan, created_at_utc=NOW)


def _binding(firmware: str = '1.0'):
    return build_device_binding(
        adapter_id=FILE_ADAPTER_ID,
        device_family='generic-file-target',
        device_model='fixture-avr',
        device_serial='anon-1',
        firmware_version=firmware,
        routing=(('ch-1', 'avr-ch1'),),
        bound_at_utc=NOW,
    )


def _matrix() -> DeviceCompatibilityMatrix:
    rows = (
        DeviceCompatibilityRow(
            adapter_id=FILE_ADAPTER_ID,
            adapter_version=FILE_ADAPTER_VERSION,
            manufacturer='Generic',
            model='fixture-avr',
            firmware_version='1.0',
            transport='file',
            capability='peq',
            direction='export',
            tier='FIXTURE_VERIFIED',
            interface_provenance='documented_file_format',
            qualified_at_utc=NOW,
        ),
        DeviceCompatibilityRow(
            adapter_id=FILE_ADAPTER_ID,
            adapter_version=FILE_ADAPTER_VERSION,
            manufacturer='Generic',
            model='fixture-avr',
            firmware_version='1.0',
            transport='file',
            capability='readback',
            direction='read',
            tier='FIXTURE_VERIFIED',
            interface_provenance='documented_file_format',
            qualified_at_utc=NOW,
        ),
    )
    payload = {
        'matrix_id': 'matrix-1',
        'created_at_utc': NOW,
        'rows': rows,
    }
    provisional = DeviceCompatibilityMatrix.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return DeviceCompatibilityMatrix.model_validate(
        {
            **payload,
            'semantic_sha256': hashlib.sha256(
                json.dumps(
                    provisional.identity_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )


def test_matrix_lookup_and_firmware_drift() -> None:
    matrix = _matrix()
    resolution = resolve_firmware_capability(
        matrix,
        adapter_id=FILE_ADAPTER_ID,
        model='fixture-avr',
        observed_firmware='1.0',
        capability='peq',
        direction='export',
    )
    assert resolution.state == 'FIXTURE_VERIFIED'
    assert resolution.row is not None

    drifted = resolve_firmware_capability(
        matrix,
        adapter_id=FILE_ADAPTER_ID,
        model='fixture-avr',
        observed_firmware='2.26',
        capability='peq',
        direction='export',
    )
    assert drifted.state == 'NEEDS_REQUALIFICATION'
    assert '1.0' in drifted.note

    unknown = resolve_firmware_capability(
        matrix,
        adapter_id=FILE_ADAPTER_ID,
        model='fixture-avr',
        observed_firmware='1.0',
        capability='mute',
        direction='write',
    )
    assert unknown.state == 'UNKNOWN'


def test_hardware_tier_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        DeviceCompatibilityRow(
            adapter_id='a',
            adapter_version='1',
            manufacturer='m',
            model='d',
            firmware_version='1',
            transport='net',
            capability='peq',
            direction='write',
            tier='HARDWARE_VERIFIED',
            interface_provenance='official_public_api',
        )


def test_qualification_record_is_hashed() -> None:
    payload = {
        'record_id': 'qual-1',
        'adapter_id': FILE_ADAPTER_ID,
        'adapter_version': FILE_ADAPTER_VERSION,
        'model': 'RX-A4A',
        'firmware_version': '2.24',
        'app_version': '0.2.0',
        'connection_mode': 'file',
        'capability_results': (('peq', 'export', 'PASS'),),
        'qualified_at_utc': NOW,
    }
    provisional = DeviceQualificationRecord.model_construct(
        **payload, record_sha256='0' * 64
    )
    record = DeviceQualificationRecord.model_validate(
        {
            **payload,
            'record_sha256': hashlib.sha256(
                json.dumps(
                    provisional.identity_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )
    assert record.firmware_version == '2.24'


def test_conformance_harness_over_file_adapter(tmp_path) -> None:
    adapter = FileCalibrationAdapter(tmp_path)
    export = _export()
    binding = _binding()
    wrong_binding = build_device_binding(
        adapter_id='other-adapter',
        device_family='x',
        device_model='y',
        device_serial='z',
        firmware_version='1',
        routing=(('ch-1', 'o'),),
        bound_at_utc=NOW,
    )
    cases = (
        ConformanceCase(
            case_id='cap-1',
            capability='bind_exact_target',
            direction='read',
            operation='capability_report',
            expected={
                'adapter_kind': 'offline_file',
                'supports_apply': False,
                'supports_read_back': True,
                'supports_materialization': True,
            },
        ),
        ConformanceCase(
            case_id='mat-1',
            capability='peq',
            direction='export',
            operation='materialize_deterministic',
            export=export,
            binding=binding,
        ),
        ConformanceCase(
            case_id='gate-1',
            capability='command_ack',
            direction='write',
            operation='unsupported_stage_blocked',
            export=export,
            binding=binding,
            expected={'stage': 'apply'},
        ),
        ConformanceCase(
            case_id='bind-1',
            capability='bind_exact_target',
            direction='read',
            operation='binding_mismatch_blocked',
            export=export,
            wrong_binding=wrong_binding,
        ),
    )
    report = run_conformance_harness(
        adapter, cases, generated_at_utc=NOW
    )
    assert report.adapter_id == FILE_ADAPTER_ID
    assert report.passed, [
        r.detail for r in report.results if r.status != 'PASS'
    ]
    assert report.verification_tier == 'FIXTURE_VERIFIED'


def test_readback_normalize_case(tmp_path) -> None:
    adapter = FileCalibrationAdapter(tmp_path)
    export = _export()
    binding = _binding()
    readback = {
        'binding_sha256': binding.binding_sha256,
        'channels': [
            c.model_dump(mode='json') for c in export.channels
        ],
    }
    (tmp_path / FILE_READBACK_NAME).write_text(
        json.dumps(readback), encoding='utf-8'
    )
    case = ConformanceCase(
        case_id='rb-1',
        capability='readback',
        direction='read',
        operation='readback_normalize',
        export=export,
        binding=binding,
        expected={'channel_ids': ['ch-1']},
    )
    report = run_conformance_harness(
        adapter, (case,), generated_at_utc=NOW
    )
    assert report.results[0].status == 'PASS', report.results[0].detail


def test_unsupported_items_surfaced_case(tmp_path) -> None:
    adapter = FileCalibrationAdapter(tmp_path)
    export = _export()
    # binding with no routing entries -> every channel is unsupported
    bare_binding = build_device_binding(
        adapter_id=FILE_ADAPTER_ID,
        device_family='generic-file-target',
        device_model='fixture-avr',
        device_serial='anon-1',
        firmware_version='1.0',
        routing=(),
        bound_at_utc=NOW,
    )
    case = ConformanceCase(
        case_id='uns-1',
        capability='channel_output_mapping',
        direction='export',
        operation='unsupported_items_surfaced',
        export=export,
        binding=bare_binding,
        expected={'min_unsupported_items': 1},
    )
    report = run_conformance_harness(
        adapter, (case,), generated_at_utc=NOW
    )
    assert report.results[0].status == 'PASS', report.results[0].detail
