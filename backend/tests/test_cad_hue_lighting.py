"""#1069 Hue CLIP v2 local adapter tests — fixture bridge, no hardware."""

from __future__ import annotations

import pytest

from htdt.cad_equipment_device import (
    DeviceActionRequest,
    DeviceFrameworkError,
    build_device_target_binding,
    verify_action_outcome,
)
from htdt.cad_hue_lighting import (
    FixtureHueTransport,
    HueLocalAdapter,
    kelvin_to_mirek,
    mirek_to_kelvin,
    split_endpoint,
)


NOW = '2026-09-25T00:00:00+00:00'
HOST = '192.0.2.20'

LIGHT = {
    'type': 'light',
    'metadata': {'name': 'Sconce L'},
    'product_data': {
        'model_id': 'LCA001',
        'software_version': '1.122.2',
        'manufacturer_name': 'Signify',
    },
    'on': {'on': True},
    'dimming': {'brightness': 50.0},
    'color_temperature': {'mirek': 370},
}
SCENE = {
    'type': 'scene',
    'metadata': {'name': 'Movie'},
    'mode': 'inactive',
}


def _resources():
    return {
        'light/light-1': dict(LIGHT),
        'scene/scene-1': dict(SCENE),
    }


def _binding(endpoint=None, **over):
    payload = {
        'document_id': 'doc-1',
        'installed_equipment_ref': 'inst-light-1',
        'device_kind': 'lighting_fixture',
        'adapter_id': 'htdt-hue-local',
        'adapter_version': '1',
        'endpoint': endpoint or f'hue://{HOST}/light/light-1',
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_device_target_binding(**payload)


def _adapter(resources=None, **kw):
    transport = FixtureHueTransport(
        resources if resources is not None else _resources(), **kw
    )
    return HueLocalAdapter({HOST: transport}), transport


def test_endpoint_parsing():
    assert split_endpoint('hue://br/light/abc') == ('br', 'light', 'abc')
    with pytest.raises(Exception):
        split_endpoint('hue://br/bogus/abc')


def test_mirek_kelvin_roundtrip():
    assert kelvin_to_mirek(2700) == 370
    assert mirek_to_kelvin(370) == 2703
    assert kelvin_to_mirek(100) == 500  # clamped, never out-of-range


def test_discovery_lists_bounded_resources():
    adapter, _ = _adapter()
    endpoints = adapter.discover_targets()
    assert f'hue://{HOST}/light/light-1' in endpoints
    assert f'hue://{HOST}/scene/scene-1' in endpoints


def test_observe_state_normalizes_fields():
    adapter, _ = _adapter()
    state = adapter.observe_state(_binding())
    assert state.field('on').value == 'on'
    assert state.field('brightness_pct').value == '50'
    assert state.field('cct_k').value == str(mirek_to_kelvin(370))
    assert state.field('name').value == 'Sconce L'
    assert state.field('model_id').value == 'LCA001'
    assert state.firmware_version == '1.122.2'
    assert state.raw_source_sha256 is not None


def test_probe_marks_bounded_surface():
    adapter, _ = _adapter()
    snapshot = adapter.probe_capabilities(_binding())
    assert snapshot.capability_state('set_on') == 'supported'
    assert snapshot.capability_state('set_brightness') == 'supported'
    assert snapshot.capability_state('set_cct') == 'supported'
    assert (
        snapshot.capability_state(
            'gradients_entertainment_dynamic_scenes'
        ) == 'unsupported'
    )


def test_unbound_host_rejected():
    adapter, _ = _adapter()
    binding = _binding(endpoint='hue://other-host/light/x')
    with pytest.raises(DeviceFrameworkError):
        adapter.observe_state(binding)


def test_apply_then_readback_verifies():
    adapter, transport = _adapter()
    binding = _binding()
    request = DeviceActionRequest(
        request=(('on', 'off'), ('brightness_pct', '25'))
    )
    action = adapter.plan_action(binding, request)
    assert action is not None and action.unsupported == ()
    ack = adapter.apply_action(binding, action, operator_confirmed=False)
    assert ack is not None and not ack.accepted

    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and ack.accepted
    put = [r for r in transport.requests if r[1] == 'PUT']
    assert put and put[0][3] == {
        'on': {'on': False},
        'dimming': {'brightness': 25.0},
    }
    readback = adapter.read_back(binding)
    assert verify_action_outcome(action, ack, readback) == 'verified'
    assert readback.field('on').value == 'off'
    assert readback.field('brightness_pct').value == '25'


def test_scene_recall_only_on_scene_resources():
    adapter, _ = _adapter()
    scene_binding = _binding(
        endpoint=f'hue://{HOST}/scene/scene-1',
        device_kind='lighting_controller',
    )
    request = DeviceActionRequest(request=(('scene_recall', 'active'),))
    action = adapter.plan_action(scene_binding, request)
    assert action is not None and action.unsupported == ()
    # Recall on a light binding is unsupported.
    light_action = adapter.plan_action(_binding(), request)
    assert light_action is not None
    assert light_action.unsupported == ('scene_recall',)


def test_events_are_evidence_not_truth():
    adapter, transport = _adapter(
        events=(
            {
                'type': 'update',
                'data': [
                    {
                        'id': 'light-1',
                        'type': 'light',
                        'on': {'on': False},
                        'dimming': {'brightness': 10.0},
                    }
                ],
            },
        )
    )
    records = adapter.collect_events(HOST, received_at_utc=NOW)
    assert len(records) == 1
    assert records[0].resource_type == 'light'
    assert records[0].normalized == {'on': 'off', 'brightness_pct': '10'}
