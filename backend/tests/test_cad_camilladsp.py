"""#1072 CamillaDSP importer + live adapter tests — fixture transport."""

from __future__ import annotations

import json

import pytest

from htdt.cad_camilladsp import (
    CamillaDSPAdapter,
    CamillaDSPError,
    FixtureCamillaDSPTransport,
    build_camilladsp_artifact,
    load_camilladsp_config,
)
from htdt.cad_equipment_device import (
    DeviceActionRequest,
    build_device_target_binding,
    verify_action_outcome,
)


NOW = '2026-09-25T00:00:00+00:00'

YAML_CONFIG = """\
devices:
  samplerate: 48000
  playback:
    type: Wasapi
    channels: 2
mixers:
  passthrough:
    channels:
      in: 2
      out: 2
    mapping:
      - dest: 0
        sources: [{channel: 0, gain: 0, inverted: false}]
filters:
  peq_left:
    type: Biquad
    parameters: {type: Peaking, freq: 105, gain: -4.5, q: 1.1}
  room_gain:
    type: Gain
    parameters: {gain: -2.0}
  sub_delay:
    type: Delay
    parameters: {delay: 3.2, unit: ms}
  room_fir:
    type: Conv
    parameters: {type: Raw, filename: "ir_left.wav"}
  mystery:
    type: Volume
    parameters: {ramp_time: 150}
pipeline:
  - type: Mixer
    name: passthrough
  - type: Filter
    channel: 0
    names: [peq_left, room_gain, sub_delay, room_fir, mystery]
  - type: Filter
    channel: 1
    names: [peq_left]
"""

BAD_YAML = 'devices: [\n'


def test_load_config_yaml_and_json():
    config = load_camilladsp_config(YAML_CONFIG.encode())
    assert config['devices']['samplerate'] == 48000
    json_config = load_camilladsp_config(b'{"devices": {"samplerate": 96}}')
    assert json_config['devices']['samplerate'] == 96
    with pytest.raises(CamillaDSPError):
        load_camilladsp_config(BAD_YAML.encode())


def test_import_normalizes_pipeline_channels():
    artifact = build_camilladsp_artifact(
        YAML_CONFIG.encode(),
        source_filename='camilladsp.yml',
        imported_at_utc=NOW,
        artifact_id='imported-calibration:test',
        channel_map={'playback:0': 'ch-L'},
    )
    assert artifact.producer_tool == 'camilladsp'
    by_label = {c.channel_label: c for c in artifact.channels}
    left = by_label['playback:0']
    assert left.preamp_db == -2.0
    assert left.delay_s == pytest.approx(0.0032)
    assert len(left.peq) == 1
    band = left.peq[0]
    assert band.filter_type == 'peaking'
    assert band.frequency_hz == 105
    assert band.gain_db == -4.5
    assert band.q == pytest.approx(1.1)
    # Channel 1 got the same filter name; right channel declared.
    assert 'playback:1' in by_label
    # Convolution file is a tracked dependency, not interpreted.
    assert artifact.file_dependencies
    assert artifact.file_dependencies[0].include_path == 'ir_left.wav'
    assert not artifact.file_dependencies[0].resolved
    kinds = {s.kind for s in artifact.opaque_sections}
    # mixer + processor/step + Conv + Volume stay opaque, never dropped.
    assert 'external_file_reference' in kinds
    assert 'unsupported_filter_type' in kinds
    # Mapping records the explicit channel binding.
    entry = {m.channel_label: m for m in artifact.channel_mapping}
    assert entry['playback:0'].htdt_channel_id == 'ch-L'
    assert entry['playback:1'].state == 'unmapped'


def _binding(endpoint='camilladsp://192.0.2.30:1234', **over):
    payload = {
        'document_id': 'doc-1',
        'installed_equipment_ref': 'inst-dsp-1',
        'device_kind': 'dsp',
        'adapter_id': 'htdt-camilladsp',
        'adapter_version': '1',
        'endpoint': endpoint,
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_device_target_binding(**payload)


def _adapter(config=None, **kw):
    transport = FixtureCamillaDSPTransport(
        config=config if config is not None else {'devices': {'samplerate': 48000}},
        **kw,
    )
    return CamillaDSPAdapter({'camilladsp://192.0.2.30:1234': transport}), transport


def test_probe_and_observe_live_state():
    adapter, _ = _adapter()
    binding = _binding()
    snapshot = adapter.probe_capabilities(binding)
    assert snapshot.capability_state('getversion') == 'supported'
    state = adapter.observe_state(binding)
    assert state.field('camilladsp_version').value == '3.0.0'
    assert state.field('state').value == 'Running'
    canonical = json.dumps(
        {'devices': {'samplerate': 48000}},
        sort_keys=True, separators=(',', ':'),
    )
    assert state.field('config_json').value == canonical
    assert state.firmware_version == '3.0.0'


def test_validate_gates_apply_and_readback_verifies():
    adapter, transport = _adapter()
    binding = _binding()
    desired = {'devices': {'samplerate': 96000}}
    canonical = json.dumps(desired, sort_keys=True, separators=(',', ':'))
    request = DeviceActionRequest(request=(('config_json', canonical),))
    action = adapter.plan_action(binding, request)
    assert action is not None and action.unsupported == ()

    ack = adapter.apply_action(binding, action, operator_confirmed=False)
    assert ack is not None and not ack.accepted

    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and ack.accepted
    commands = [next(iter(r)) for r in transport.requests]
    # Validation ran before the mutation.
    assert commands.index('ValidateConfigJson') < commands.index('SetConfigJson')
    assert verify_action_outcome(
        action, ack, adapter.read_back(binding)
    ) == 'verified'


def test_failed_validation_blocks_apply():
    adapter, _ = _adapter(reject_config='missing playback device')
    binding = _binding()
    canonical = json.dumps({'devices': {}}, separators=(',', ':'))
    request = DeviceActionRequest(request=(('config_json', canonical),))
    action = adapter.plan_action(binding, request)
    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and not ack.accepted
    assert 'device_error' in (ack.detail or '')


def test_reload_command_and_unsupported_fields():
    adapter, transport = _adapter()
    binding = _binding()
    request = DeviceActionRequest(
        request=(('reload', 'true'), ('preset_name', 'foo'))
    )
    action = adapter.plan_action(binding, request)
    assert action is not None
    assert action.unsupported == ('preset_name',)
    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and ack.accepted
    assert any('Reload' in r for r in transport.requests)


# ----------------------------------------------------------------------
# Equalizer APO importer extensions for #1072 (Convolution + conditionals)

from htdt.cad_external_calibration import build_equalizer_apo_artifact

EAPO_CONFIG = """\
Device: Speakers
Channel: L
Preamp: -3.0 dB
Filter  1: ON PK Fc 105 Hz Gain -4.5 dB Q 1.1
Delay: 3.2 ms
Convolution: ir_left.wav
If: channelCount == 2
Filter  2: ON PK Fc 200 Hz Gain -2 dB Q 2
EndIf:
Include: extra.txt
"""

EAPO_EXTRA = 'Filter  9: ON PK Fc 40 Hz Gain 1 dB Q 3\n'


def test_convolution_recorded_as_file_dependency():
    artifact = build_equalizer_apo_artifact(
        EAPO_CONFIG.encode(),
        source_filename='config.txt',
        imported_at_utc=NOW,
        artifact_id='imported-calibration:eapo',
        include_resolver=lambda path: (
            ('extra.txt', EAPO_EXTRA.encode())
            if path == 'extra.txt'
            else ('ir_left.wav', b'RIFF....') if path == 'ir_left.wav'
            else None
        ),
    )
    assert artifact.file_dependencies
    dep = artifact.file_dependencies[0]
    assert dep.include_path == 'ir_left.wav'
    assert dep.resolved
    assert dep.source_sha256 is not None
    kinds = {s.kind for s in artifact.opaque_sections}
    assert 'external_file_reference' in kinds
    # Conditional block contents are blocked, not interpreted.
    assert 'conditional_block' in kinds
    assert any(
        'conditional' in d for d in artifact.diagnostics
    )
    # Unconditional Filter 1 lands on L; the included Filter 9 has no
    # channel scope of its own so it lands on the ALL bucket.
    by_label = {c.channel_label: c for c in artifact.channels}
    assert len(by_label['L'].peq) == 1
    assert by_label['L'].preamp_db == -3.0
    assert by_label['L'].delay_s == pytest.approx(0.0032)
    assert len(by_label['ALL'].peq) == 1
    # Include still resolves.
    assert artifact.include_dependencies[0].resolved


def test_convolution_missing_file_is_unresolved_not_dropped():
    artifact = build_equalizer_apo_artifact(
        'Convolution: missing.wav\n'.encode(),
        source_filename='config.txt',
        imported_at_utc=NOW,
        artifact_id='imported-calibration:eapo2',
    )
    dep = artifact.file_dependencies[0]
    assert not dep.resolved
    assert any(
        'unresolved convolution' in d for d in artifact.diagnostics
    )
