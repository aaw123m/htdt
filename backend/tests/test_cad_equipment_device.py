"""#726 equipment device adapter / observed-state framework tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_equipment_device import (
    DeviceActionRequest,
    DeviceCapabilityEntry,
    DeviceFrameworkError,
    FixtureDeviceAdapter,
    build_device_target_binding,
    verify_action_outcome,
)
from htdt.cad_equipment_device_repository import (
    CadEquipmentDeviceRepository,
    DeviceFrameworkConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene

NOW = '2026-09-23T00:00:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadEquipmentDeviceRepository(
        scene_repository
    )


def _binding(revision, adapter: FixtureDeviceAdapter, endpoint='avr-1', **over):
    payload = {
        'document_id': revision.document_id,
        'installed_equipment_ref': 'inst-avr-1',
        'device_kind': 'avr',
        'adapter_id': adapter.adapter_id,
        'adapter_version': adapter.adapter_version,
        'endpoint': endpoint,
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_device_target_binding(**payload)


def _adapter():
    return FixtureDeviceAdapter(
        targets={
            'avr-1': {
                'firmware': '1.42',
                'volume': '-20.0',
                'input': 'bluray',
            },
        },
    )


def test_binding_is_explicit_and_credential_free(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    repository.save_binding(binding)
    assert repository.get_binding(binding.binding_id) == binding

    # A credential reference is a secret *name*; a value lookalike is rejected.
    with pytest.raises(ValidationError):
        _binding(revision, adapter, credential_ref='password=hunter2')
    # Bindings require a persisted document.
    with pytest.raises(ValueError, match='no persisted SceneRevision'):
        repository.save_binding(
            _binding(revision, adapter, document_id='ghost-doc')
        )
    # Append-only.
    with pytest.raises(DeviceFrameworkConflictError):
        repository.save_binding(binding)


def test_capability_and_observed_state_are_separate(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    repository.save_binding(binding)

    snapshot = adapter.probe_capabilities(binding)
    repository.save_capability_snapshot(snapshot)
    assert snapshot.capability_state('peq') == 'unsupported'
    assert snapshot.capability_state('hdmi_board') == 'unknown'

    observation = adapter.observe_state(binding)
    repository.save_observation(observation)
    assert observation.field('volume').value == '-20.0'
    # Fixture 'firmware' is not a declared capability -> explicit unsupported,
    # never a silently filled value.
    assert observation.field('firmware').state == 'unsupported'
    assert observation.field('firmware').value is None

    # A second observation is a new row — history is never rewritten.
    later = adapter.observe_state(binding)
    repository.save_observation(later)
    stored = repository.list_observations(binding.binding_sha256)
    assert len(stored) == 2
    assert {item.observation_id for item in stored} == {
        observation.observation_id,
        later.observation_id,
    }


def test_ack_is_not_verified_state(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    repository.save_binding(binding)

    request = DeviceActionRequest(
        request=(('volume', '-15.0'),), reason='raise reference level'
    )
    action = adapter.plan_action(binding, request)
    assert action is not None
    repository.save_action(action)

    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and ack.accepted
    repository.save_ack(ack)

    # ACK alone says nothing about effective state.
    assert verify_action_outcome(action, ack, None) == 'ack_only'

    # Read-back proves it.
    observed = adapter.read_back(binding)
    assert observed is not None
    assert verify_action_outcome(action, ack, observed) == 'verified'


def test_unconfirmed_action_is_not_accepted(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    repository.save_binding(binding)
    request = DeviceActionRequest(request=(('volume', '-15.0'),))
    action = adapter.plan_action(binding, request)
    assert action is not None
    repository.save_action(action)
    ack = adapter.apply_action(binding, action, operator_confirmed=False)
    assert ack is not None and not ack.accepted
    repository.save_ack(ack)
    assert verify_action_outcome(action, ack, None) == 'not_accepted'


def test_unsupported_fields_are_surfaced_before_apply(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    repository.save_binding(binding)
    request = DeviceActionRequest(
        request=(('peq_band_1', '+3'), ('volume', '-15.0'))
    )
    action = adapter.plan_action(binding, request)
    assert action is not None
    # The unsupported field is called out, not silently dropped.
    assert action.unsupported == ('peq_band_1',)
    assert [command.field for command in action.commands] == ['volume']


def test_no_fuzzy_fallback_between_devices(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter, endpoint='avr-2')
    repository.save_binding(binding)
    # Endpoint not bound to this adapter -> hard error, never a fallback.
    with pytest.raises(DeviceFrameworkError):
        adapter.observe_state(binding)


def test_records_require_persisted_parents(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    # Snapshot for a binding that was never saved -> rejected.
    snapshot = adapter.probe_capabilities(binding)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_capability_snapshot(snapshot)
    observation = adapter.observe_state(binding)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_observation(observation)


def test_observation_hash_integrity(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    adapter = _adapter()
    binding = _binding(revision, adapter)
    observation = adapter.observe_state(binding)
    payload = observation.model_dump(mode='python')
    payload['fields'] = ()
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(observation)(**payload)
