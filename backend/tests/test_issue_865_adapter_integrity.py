"""#865: the framework never trusts an adapter's returned record solely
because its self-hash is valid, and observed-state project identity is
derived from persisted calibration authority.

- materialize(): the returned record must equal the requested
  adapter/version/export/binding authorities;
- apply(): same provenance checks + the ACK must bind the exact requested
  materialization;
- read-back/operator snapshots: document identity derives from the exact
  persisted CalibrationExport→CalibrationPlan chain, and observed channels
  must belong to the export or bound routing — never arbitrary caller
  document ids or foreign channels.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
from htdt.cad_device_adapter import (
    AdapterResultMismatchError,
    CalibrationAdapterService,
    DeviceApplyAck,
    _hash,
    build_device_binding,
)
from htdt.cad_device_adapter_file import (
    FILE_ADAPTER_ID,
    FILE_ADAPTER_VERSION,
    FILE_READBACK_NAME,
    FileCalibrationAdapter,
)

NOW = '2026-09-24T00:00:00+00:00'


class _CalibrationAuthority:
    """Stub resolving the exact persisted export and owning plan."""

    def __init__(self, plan, export) -> None:
        self._plan = plan
        self._export = export

    def get_export(self, export_id):
        return self._export if export_id == self._export.export_id else None

    def get_plan(self, plan_id):
        return self._plan if plan_id == self._plan.plan_id else None


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        channel_gain_resolution_db=0.5,
    )


def _plan(document_id='doc-a') -> CadCalibrationPlan:
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
        'plan_id': f'plan-{document_id}',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': document_id,
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
            'plan_semantic_sha256': sha256(
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


def _export(document_id='doc-a'):
    return build_generic_biquad_export(
        plan=_plan(document_id), created_at_utc=NOW
    )


def _binding(**overrides):
    values = dict(
        adapter_id=FILE_ADAPTER_ID,
        device_family='avr-family',
        device_model='AVR-X1000',
        device_serial='SN-42',
        firmware_version='1.2.3',
        routing=(('ch-1', 'out-1'),),
        bound_at_utc=NOW,
    )
    values.update(overrides)
    return build_device_binding(**values)


def _service(tmp_path: Path, adapter=None, document_id='doc-a'):
    adapter = (
        adapter if adapter is not None else FileCalibrationAdapter(tmp_path)
    )
    export = _export(document_id)
    return (
        CalibrationAdapterService(
            adapter,
            calibration_repository=_CalibrationAuthority(
                _plan(document_id), export
            ),
        ),
        export,
    )


def _rewritten(materialization, **overrides):
    """A fully self-consistent materialization carrying false claims — the
    kind a buggy adapter could return and a hash check alone cannot catch."""
    fields = {
        name: getattr(materialization, name)
        for name in type(materialization).model_fields
    }
    fields.update(overrides)
    forged = materialization.model_construct(**fields)
    return forged.model_copy(
        update={
            'materialization_sha256': _hash(forged.semantic_payload())
        }
    )


# -- materialize postconditions ------------------------------------------------


def _lying_materialize(overrides):
    class _LyingAdapter(FileCalibrationAdapter):
        def materialize(self, export, binding, *, created_at_utc):
            honest = super().materialize(
                export, binding, created_at_utc=created_at_utc
            )
            return _rewritten(honest, **overrides)

    return _LyingAdapter


def test_materialize_wrong_export_rejected(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out',
        adapter=_lying_materialize({'export_id': 'other-export'})(tmp_path / 'out'),
    )
    with pytest.raises(AdapterResultMismatchError, match='export_id'):
        service.materialize_export(export, _binding(), created_at_utc=NOW)


def test_materialize_wrong_adapter_version_rejected(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out',
        adapter=_lying_materialize({'adapter_version': 'tampered-9'})(
            tmp_path / 'out'
        ),
    )
    with pytest.raises(AdapterResultMismatchError, match='adapter_version'):
        service.materialize_export(export, _binding(), created_at_utc=NOW)


def test_materialize_wrong_binding_rejected(tmp_path: Path) -> None:
    other = _binding(binding_id='binding-2')
    service, export = _service(
        tmp_path / 'out',
        adapter=_lying_materialize(
            {
                'binding_id': other.binding_id,
                'binding_sha256': other.binding_sha256,
            }
        )(tmp_path / 'out'),
    )
    with pytest.raises(AdapterResultMismatchError, match='binding'):
        service.materialize_export(export, _binding(), created_at_utc=NOW)


def test_materialize_wrong_export_hash_rejected(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out',
        adapter=_lying_materialize(
            {'exported_settings_semantic_sha256': 'f' * 64}
        )(tmp_path / 'out'),
    )
    with pytest.raises(AdapterResultMismatchError, match='export'):
        service.materialize_export(export, _binding(), created_at_utc=NOW)


# -- apply provenance -----------------------------------------------------------


def _applyable(adapter_dir, ack_materialization_id=None):
    class _ApplyableAdapter(FileCalibrationAdapter):
        def capability(self):
            return super().capability().model_copy(
                update={'supports_apply': True}
            )

        def apply(self, materialization, binding, *, operator_confirmed, applied_at_utc):
            return DeviceApplyAck(
                ack_id='ack-1',
                materialization_id=(
                    ack_materialization_id
                    if ack_materialization_id is not None
                    else materialization.materialization_id
                ),
                acked_at_utc=applied_at_utc,
            )

    return _ApplyableAdapter(adapter_dir)


def test_ack_for_other_materialization_rejected(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out',
        adapter=_applyable(
            tmp_path / 'out', ack_materialization_id='mat:other'
        ),
    )
    binding = _binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    with pytest.raises(AdapterResultMismatchError, match='ack'):
        service.apply_materialization(
            materialization,
            binding,
            operator_confirmed=True,
            applied_at_utc=NOW,
        )


def test_apply_rejects_foreign_adapter_materialization(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out', adapter=_applyable(tmp_path / 'out')
    )
    binding = _binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    forged = _rewritten(materialization, adapter_id='other-adapter')
    with pytest.raises(AdapterResultMismatchError, match='adapter'):
        service.apply_materialization(
            forged,
            binding,
            operator_confirmed=True,
            applied_at_utc=NOW,
        )


def test_apply_rejects_unpersisted_export(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out', adapter=_applyable(tmp_path / 'out')
    )
    binding = _binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    forged = _rewritten(
        materialization,
        export_id='not-persisted',
        exported_settings_semantic_sha256='f' * 64,
    )
    with pytest.raises(AdapterResultMismatchError, match='not persisted'):
        service.apply_materialization(
            forged,
            binding,
            operator_confirmed=True,
            applied_at_utc=NOW,
        )


def test_apply_happy_path(tmp_path: Path) -> None:
    service, export = _service(
        tmp_path / 'out', adapter=_applyable(tmp_path / 'out')
    )
    binding = _binding()
    materialization = service.materialize_export(
        export, binding, created_at_utc=NOW
    )
    ack = service.apply_materialization(
        materialization,
        binding,
        operator_confirmed=True,
        applied_at_utc=NOW,
    )
    assert ack.materialization_id == materialization.materialization_id


# -- observed-state project identity --------------------------------------------


def test_snapshot_document_derived_from_authority(tmp_path: Path) -> None:
    service, export = _service(tmp_path / 'out', document_id='doc-a')
    binding = _binding()
    observed = export.channels[0].model_copy(update={'gain_db': 2.0})
    (tmp_path / 'out').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'out' / FILE_READBACK_NAME).write_text(
        json.dumps(
            {
                'binding_sha256': binding.binding_sha256,
                'channels': [observed.model_dump(mode='json')],
            }
        ),
        encoding='utf-8',
    )
    snapshot = service.read_back_snapshot(
        binding, export, observed_at_utc=NOW
    )
    assert snapshot.document_id == 'doc-a'


def test_cross_project_observation_rejected(tmp_path: Path) -> None:
    service, export = _service(tmp_path / 'out', document_id='doc-a')
    binding = _binding()
    (tmp_path / 'out').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'out' / FILE_READBACK_NAME).write_text(
        json.dumps(
            {
                'binding_sha256': binding.binding_sha256,
                'channels': [
                    c.model_dump(mode='json') for c in export.channels
                ],
            }
        ),
        encoding='utf-8',
    )
    with pytest.raises(AdapterResultMismatchError, match='doc-b|document'):
        service.read_back_snapshot(
            binding, export, document_id='doc-b', observed_at_utc=NOW
        )
    with pytest.raises(AdapterResultMismatchError, match='doc-b|document'):
        service.record_operator_snapshot(
            binding,
            export,
            document_id='doc-b',
            observed_channels=export.channels,
            observed_at_utc=NOW,
        )


def test_unpersisted_export_observation_rejected(tmp_path: Path) -> None:
    service, export = _service(tmp_path / 'out')
    foreign_export = _export('doc-b')  # never persisted in this authority
    with pytest.raises(AdapterResultMismatchError, match='persisted'):
        service.record_operator_snapshot(
            _binding(),
            foreign_export,
            observed_channels=foreign_export.channels,
            observed_at_utc=NOW,
        )


def test_observed_foreign_channel_rejected(tmp_path: Path) -> None:
    service, export = _service(tmp_path / 'out')
    foreign = export.channels[0].model_copy(update={'channel_id': 'ch-99'})
    with pytest.raises(AdapterResultMismatchError, match='channel'):
        service.record_operator_snapshot(
            _binding(),
            export,
            observed_channels=(foreign,),
            observed_at_utc=NOW,
        )


def test_operator_snapshot_derives_document(tmp_path: Path) -> None:
    service, export = _service(tmp_path / 'out', document_id='doc-a')
    snapshot = service.record_operator_snapshot(
        _binding(),
        export,
        observed_channels=export.channels,
        observed_at_utc=NOW,
    )
    assert snapshot.document_id == 'doc-a'
    assert snapshot.deviations == ()


def test_snapshot_requires_calibration_authority(tmp_path: Path) -> None:
    service = CalibrationAdapterService(
        FileCalibrationAdapter(tmp_path / 'out')
    )
    with pytest.raises(Exception, match='calibration repository'):
        service.record_operator_snapshot(
            _binding(),
            _export(),
            observed_channels=_export().channels,
            observed_at_utc=NOW,
        )


# -- installed-equipment binding ------------------------------------------------


def test_installed_equipment_binding_additive() -> None:
    bound = build_device_binding(
        adapter_id=FILE_ADAPTER_ID,
        device_family='avr-family',
        device_model='AVR-X1000',
        device_serial='SN-42',
        firmware_version='1.2.3',
        routing=(('ch-1', 'out-1'),),
        bound_at_utc=NOW,
        installed_equipment_instance_id='inst-eq-77',
    )
    assert bound.installed_equipment_instance_id == 'inst-eq-77'
    assert 'installed_equipment_instance_id' in bound.semantic_payload()
    # Older bindings without the field remain valid (additive identity).
    legacy = _binding()
    assert legacy.installed_equipment_instance_id is None
    assert 'installed_equipment_instance_id' not in legacy.semantic_payload()
    assert bound.adapter_id == FILE_ADAPTER_ID
    assert FILE_ADAPTER_VERSION
