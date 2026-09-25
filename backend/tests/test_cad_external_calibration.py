from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationExportSnapshot,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_biquad_filter,
    build_generic_biquad_export,
)
from htdt.cad_external_calibration import (
    build_equalizer_apo_artifact,
    compare_imported_vs_exported,
)

NOW = '2026-09-24T00:00:00+00:00'

BASIC_CONFIG = b"""# comment line
Preamp: 1 dB
Channel: L
Filter 1: ON PK Fc 100 Hz Gain -3 dB Q 1.4
Delay: 1 ms
"""


def _build(
    source: bytes = BASIC_CONFIG,
    channel_map: dict[str, str] | None = None,
    include_resolver=None,
) -> object:
    return build_equalizer_apo_artifact(
        source,
        source_filename='config.txt',
        imported_at_utc=NOW,
        artifact_id='imported-calibration:test-1',
        channel_map=channel_map,
        include_resolver=include_resolver,
    )


def _constraints() -> CadDeviceCapabilityConstraints:
    return CadDeviceCapabilityConstraints(
        capability_id='test-device-1',
        capability_version='1',
        supported_sample_rates_hz=(48000,),
        supported_filter_types=('peaking',),
        channel_gain_resolution_db=0.5,
    )


def _export(
    gain_db: float = 1.0,
    delay_s: float = 0.001,
    peq=(),
) -> CadCalibrationExportSnapshot:
    channel = CadCalibrationChannel(
        channel_id='ch-1',
        role_id='FL',
        source_entity_id='spk-fl',
        physical_output_id='out-1',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=delay_s,
        polarity='normal',
        crossovers=(),
        peq=peq,
        routing=('avr-ch1',),
    )
    import hashlib
    import json

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


def test_minimal_config_normalizes_settings() -> None:
    artifact = _build(channel_map={'L': 'ch-1'})
    assert artifact.state == 'imported_configuration'
    assert artifact.producer_tool == 'equalizer_apo'
    assert artifact.global_preamp_db == pytest.approx(1.0)
    assert len(artifact.channels) == 1
    channel = artifact.channels[0]
    assert channel.channel_label == 'L'
    assert channel.delay_s == pytest.approx(0.001)
    assert len(channel.peq) == 1
    band = channel.peq[0]
    assert band.filter_type == 'peaking'
    assert band.frequency_hz == pytest.approx(100.0)
    assert band.gain_db == pytest.approx(-3.0)
    assert band.q == pytest.approx(1.4)
    assert artifact.channel_mapping[0].state == 'mapped'


def test_unknown_commands_surface_as_opaque() -> None:
    artifact = _build(
        b'Convolution: ir.wav\nMute: 1\nGraphicEQ: 25 0; 40 3\n'
    )
    kinds = {section.kind for section in artifact.opaque_sections}
    assert kinds == {'unsupported_command'}
    assert len(artifact.opaque_sections) == 3
    assert any('Convolution' in s.raw_text for s in artifact.opaque_sections)


def test_unsupported_filter_type_is_opaque_not_dropped() -> None:
    artifact = _build(b'Channel: L\nFilter 1: ON Modal Fc 40 Hz\n')
    assert artifact.channels[0].peq == ()
    assert artifact.opaque_sections[0].kind == 'unsupported_filter_type'
    assert 'MODAL' in artifact.opaque_sections[0].reason.upper()


def test_malformed_lines_are_surfaced() -> None:
    artifact = _build(b'Preamp: loud\nDelay: soon\n')
    kinds = {section.kind for section in artifact.opaque_sections}
    assert 'malformed_line' in kinds
    assert artifact.global_preamp_db is None


def test_include_closure_and_cycle_detection() -> None:
    files = {
        'main.txt': b'Include: sub.txt\n',
        'sub.txt': b'Channel: L\nFilter 1: ON PK Fc 80 Hz Gain -2 dB Q 2\nInclude: main.txt\n',
    }

    def resolver(path: str):
        found = files.get(path)
        return (path, found) if found else None

    artifact = _build(
        files['main.txt'],
        include_resolver=resolver,
    )
    resolved = [d for d in artifact.include_dependencies if d.resolved]
    assert [d.include_path for d in resolved] == ['sub.txt', 'main.txt']
    cycled = [d for d in artifact.include_dependencies if not d.resolved]
    assert cycled and 'cycle' in cycled[0].diagnostics[0]
    assert artifact.channels[0].peq[0].frequency_hz == pytest.approx(80.0)


def test_missing_include_is_recorded_not_silent() -> None:
    artifact = _build(b'Include: missing.txt\n')
    dep = artifact.include_dependencies[0]
    assert dep.resolved is False
    assert any('unresolved include' in d for d in artifact.diagnostics)


def test_unmapped_channel_blocks_comparison() -> None:
    artifact = _build()  # no channel_map
    assert artifact.channel_mapping[0].state == 'unmapped'
    comparison = compare_imported_vs_exported(
        artifact, _export(), compared_at_utc=NOW
    )
    imported_side = [i for i in comparison.items if i.channel_id == 'L']
    assert imported_side
    assert all(item.state == 'unmapped_channel' for item in imported_side)
    assert any(
        item.state == 'missing_in_import' and item.channel_id == 'ch-1'
        for item in comparison.items
    )


def test_compare_imported_vs_exported_exact() -> None:
    artifact = _build(channel_map={'L': 'ch-1'})
    export = _export(
        peq=(
            build_biquad_filter(
                filter_id='f-1',
                filter_type='peaking',
                frequency_hz=100.0,
                q=1.4,
                gain_db=-3.0,
                sample_rate_hz=48000,
            ),
        )
    )
    comparison = compare_imported_vs_exported(
        artifact, export, compared_at_utc=NOW
    )
    states = {item.field: item.state for item in comparison.items}
    assert states['gain_db'] == 'exact_match'
    assert states['delay_s'] == 'exact_match'
    assert states['peq[0].frequency_hz'] == 'exact_match'
    assert states['peq[0].gain_db'] == 'exact_match'
    assert states['peq[0].q'] == 'exact_match'
    # fields the format does not carry are "cannot verify", not matches
    assert states['polarity'] == 'missing_in_import'
    assert states['routing'] == 'missing_in_import'


def test_compare_value_differs() -> None:
    artifact = _build(channel_map={'L': 'ch-1'})
    export = _export(gain_db=0.5, delay_s=0.002)
    comparison = compare_imported_vs_exported(
        artifact, export, compared_at_utc=NOW
    )
    states = {item.field: item.state for item in comparison.items}
    assert states['gain_db'] == 'value_differs'
    assert states['delay_s'] == 'value_differs'


def test_artifact_is_never_applied_state() -> None:
    artifact = _build()
    assert artifact.state == 'imported_configuration'
    with pytest.raises(ValidationError):
        type(artifact).model_validate(
            {
                **artifact.identity_payload(),
                'state': 'applied',
                'semantic_sha256': artifact.semantic_sha256,
            }
        )


def test_deterministic_semantic_identity() -> None:
    first = _build()
    second = _build()
    assert first.semantic_sha256 == second.semantic_sha256
    changed = _build(BASIC_CONFIG + b'Preamp: 0 dB\n')
    assert changed.semantic_sha256 != first.semantic_sha256
