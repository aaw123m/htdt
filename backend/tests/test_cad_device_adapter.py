from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationExportSnapshot,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_device_adapter import (
    AdapterCapabilityError,
    CalibrationAdapterService,
    DeviceBindingMismatchError,
    build_device_binding,
    build_observation,
    diff_observed_vs_exported,
)
from htdt.cad_device_adapter_file import (
    FILE_ADAPTER_ID,
    FILE_READBACK_NAME,
    FileCalibrationAdapter,
)

NOW = '2026-09-24T00:00:00+00:00'


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        channel_gain_resolution_db=0.5,
    )


def _plan() -> CadCalibrationPlan:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='out-1',
        sample_rate_hz=48000,
        gain_db=1.23,
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
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': __import__('hashlib').sha256(
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


def _export() -> CadCalibrationExportSnapshot:
    return build_generic_biquad_export(plan=_plan(), created_at_utc=NOW)


def _binding(routing=(('ch-1', 'out-1'),)) -> object:
    return build_device_binding(
        adapter_id=FILE_ADAPTER_ID,
        device_family='avr-family',
        device_model='AVR-X1000',
        device_serial='SN-42',
        firmware_version='1.2.3',
        routing=routing,
        bound_at_utc=NOW,
    )


# -- binding -------------------------------------------------------------------


def test_device_binding_identity_and_uniqueness() -> None:
    binding = _binding()
    assert len(binding.binding_sha256) == 64
    with pytest.raises(ValidationError, match='unique'):
        build_device_binding(
            adapter_id=FILE_ADAPTER_ID,
            device_family='f',
            device_model='m',
            device_serial='s',
            firmware_version='v',
            routing=(('ch-1', 'o'), ('ch-1', 'o2')),
            bound_at_utc=NOW,
        )


# -- file adapter ---------------------------------------------------------------


def test_materialize_writes_deterministic_payload(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    export = _export()
    binding = _binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    assert materialization.adapter_id == FILE_ADAPTER_ID
    assert materialization.payload_text
    written = tmp_path / 'out' / f'{materialization.materialization_id}.json'
    assert json.loads(written.read_text(encoding='utf-8'))['export_id'] == export.export_id
    # Quantization notes from the export surface before apply.
    assert materialization.quantization_applied
    assert any('quantized' in note for note in materialization.quantization_notes)
    # Deterministic id for same export+binding.
    again = service.materialize_export(export, binding, created_at_utc=NOW)
    assert again.materialization_id == materialization.materialization_id


def test_materialize_flags_unmapped_channels(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    materialization = service.materialize_export(
        _export(), _binding(routing=(('other-ch', 'out-9'),)), created_at_utc=NOW
    )
    assert materialization.unsupported_items == ('ch-1: no routing on bound device',)


def test_apply_is_not_supported_and_never_silent(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    binding = _binding()
    materialization = service.materialize_export(
        _export(), binding, created_at_utc=NOW
    )
    with pytest.raises(AdapterCapabilityError, match='cannot apply'):
        service.apply_materialization(
            materialization, binding,
            operator_confirmed=True, applied_at_utc=NOW,
        )


def test_apply_requires_operator_confirmation(tmp_path: Path) -> None:
    class _ConfirmableAdapter(FileCalibrationAdapter):
        def capability(self):
            report = super().capability()
            return report.model_copy(update={'supports_apply': True})

        def apply(self, materialization, binding, *, operator_confirmed, applied_at_utc):
            from htdt.cad_device_adapter import DeviceApplyAck
            return DeviceApplyAck(
                ack_id='ack-1',
                materialization_id=materialization.materialization_id,
                acked_at_utc=applied_at_utc,
            )

    service = CalibrationAdapterService(_ConfirmableAdapter(tmp_path / 'out'))
    binding = _binding()
    materialization = service.materialize_export(
        _export(), binding, created_at_utc=NOW
    )
    with pytest.raises(PermissionError, match='confirmation'):
        service.apply_materialization(
            materialization, binding,
            operator_confirmed=False, applied_at_utc=NOW,
        )
    ack = service.apply_materialization(
        materialization, binding,
        operator_confirmed=True, applied_at_utc=NOW,
    )
    assert ack.state == 'acknowledged'
    # An ack is never a read-back — no observed state is implied.
    assert not hasattr(ack, 'observed_channels')


def test_binding_mismatch_rejected(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    materialization = service.materialize_export(
        _export(), _binding(), created_at_utc=NOW
    )
    other = build_device_binding(
        adapter_id='other-adapter',
        device_family='f', device_model='m', device_serial='s',
        firmware_version='v', routing=(('ch-1', 'out-1'),), bound_at_utc=NOW,
    )
    with pytest.raises(DeviceBindingMismatchError):
        service.materialize_export(_export(), other, created_at_utc=NOW)
    wrong = _binding(routing=(('ch-1', 'out-9'),))
    with pytest.raises(DeviceBindingMismatchError):
        service.apply_materialization(
            materialization, wrong,
            operator_confirmed=True, applied_at_utc=NOW,
        )


def test_read_back_observation(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    export = _export()
    binding = _binding()
    with pytest.raises(AdapterCapabilityError, match='no read-back'):
        service.read_back_snapshot(
            binding, export, document_id='doc-1', observed_at_utc=NOW
        )
    observed = export.channels[0].model_copy(update={'gain_db': 2.0})
    (tmp_path / 'out').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'out' / FILE_READBACK_NAME).write_text(
        json.dumps(
            {
                'binding_sha256': binding.binding_sha256,
                'channels': [observed.model_dump(mode='json')],
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    snapshot = service.read_back_snapshot(
        binding, export, document_id='doc-1', observed_at_utc=NOW
    )
    assert snapshot.source == 'read_back'
    assert snapshot.binding_sha256 == binding.binding_sha256
    assert snapshot.deviations[0].field_name == 'gain_db'
    # Drift is a new observation — the export is never rewritten.
    second = build_observation(
        document_id='doc-1',
        binding=binding,
        export=export,
        observed_channels=export.channels,
        observed_at_utc='2026-09-24T01:00:00+00:00',
        source='read_back',
    )
    assert second.deviations == ()
    assert second.snapshot_id != snapshot.snapshot_id


def test_read_back_rejects_foreign_binding(tmp_path: Path) -> None:
    adapter = FileCalibrationAdapter(tmp_path / 'out')
    service = CalibrationAdapterService(adapter)
    binding = _binding()
    (tmp_path / 'out').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'out' / FILE_READBACK_NAME).write_text(
        json.dumps(
            {'binding_sha256': '0' * 64, 'channels': []},
        ),
        encoding='utf-8',
    )
    with pytest.raises(AdapterCapabilityError, match='different device'):
        service.read_back_snapshot(
            binding, _export(), document_id='doc-1', observed_at_utc=NOW
        )


def test_diff_reports_missing_and_extra_channels() -> None:
    export = _export()
    assert diff_observed_vs_exported(export, ()) != ()
    extra = tuple(export.channels) + (
        export.channels[0].model_copy(update={'channel_id': 'ch-9'}),
    )
    deviations = diff_observed_vs_exported(export, extra)
    assert any('not exported' in (d.reason or '') for d in deviations)


def test_operator_snapshot_same_shape() -> None:
    snapshot = build_observation(
        document_id='doc-1',
        binding=_binding(),
        export=_export(),
        observed_channels=_export().channels,
        observed_at_utc=NOW,
        source='operator_entered',
    )
    assert snapshot.source == 'operator_entered'
    assert snapshot.deviations == ()
