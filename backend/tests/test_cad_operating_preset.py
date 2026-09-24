from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_operating_preset import (
    OPERATING_PRESET_NOT_CONFIGURED,
    PresetComponentRef,
    PresetProvenanceItem,
    bind_preset_measurements,
    build_operating_preset,
    evaluate_preset_freshness,
    operating_mode_summary,
    record_applied_preset_state,
)
from htdt.cad_operating_preset_repository import CadOperatingPresetRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-23T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    repository = CadOperatingPresetRepository(scene_repository)
    return scene_repository, revision, repository


def _preset(revision, **overrides):
    payload = {
        'document_id': revision.document_id,
        'name': 'Movie Night',
        'category': 'movie',
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'declared_input': 'object_audio',
        'declared_device_mode': 'Reference',
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_operating_preset(**payload)


def test_not_configured_is_the_default_mode(tmp_path: Path) -> None:
    assert OPERATING_PRESET_NOT_CONFIGURED == 'not_configured'


def test_preset_pins_exact_scene_revision_and_components(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='calibration_plan',
                ref_id='plan-1',
                ref_sha256='a' * 64,
                label='初回キャリブレーション',
            ),
            PresetComponentRef(
                kind='av_sync_condition',
                ref_id='av-1',
            ),
        ),
    )
    repository.save_preset(preset)
    stored = repository.get_preset(preset.preset_id)
    assert stored == preset
    assert stored.scene_revision_id == revision.revision_id
    assert stored.scene_content_hash == revision.content_hash


def test_preset_never_rewrites_on_component_change_evaluation(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='calibration_plan',
                ref_id='plan-1',
                ref_sha256='a' * 64,
            ),
        ),
    )
    repository.save_preset(preset)
    freshness = evaluate_preset_freshness(
        preset,
        resolve_sha256={('calibration_plan', 'plan-1'): 'b' * 64},
    )
    assert freshness.components[0].state == 'stale'
    # evaluation reports staleness; the preset itself is immutable.
    assert repository.get_preset(preset.preset_id) == preset


def test_freshness_missing_and_current(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='excitation_scenario', ref_id='scn-1', ref_sha256='c' * 64
            ),
            PresetComponentRef(kind='presentation_profile', ref_id='vid-9'),
        ),
    )
    freshness = evaluate_preset_freshness(
        preset,
        resolve_sha256={
            ('excitation_scenario', 'scn-1'): 'c' * 64,
            # presentation_profile vid-9 absent -> missing
        },
    )
    states = {item.ref_id: item.state for item in freshness.components}
    assert states == {'scn-1': 'current', 'vid-9': 'missing'}
    assert not freshness.all_current


def test_applied_state_is_distinct_from_the_preset(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(revision)
    repository.save_preset(preset)
    applied = record_applied_preset_state(
        preset,
        confirmed_at_utc='2026-09-23T01:00:00+00:00',
        device_context='avr-x4800',
        deviations=(
            PresetProvenanceItem(key='level_db', value='-15 instead of -20'),
        ),
    )
    repository.save_applied_state(applied)
    stored = repository.list_applied_states(preset.preset_id)
    assert len(stored) == 1
    assert stored[0].preset_sha256 == preset.preset_sha256
    assert stored[0].applied_id == applied.applied_id


def test_applied_state_rejects_superseded_preset(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(revision)
    repository.save_preset(preset)
    applied = record_applied_preset_state(
        preset,
        confirmed_at_utc='2026-09-23T01:00:00+00:00',
    )
    repository.save_applied_state(applied)
    forged = applied.model_copy(update={'preset_sha256': 'f' * 64})
    with pytest.raises(ValidationError):
        # model-level hash integrity makes forging impossible before storage
        forged.model_validate(forged.model_dump())


def test_measurement_binding_targets_exact_preset(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-1', 'm-2'),
        bound_at_utc='2026-09-23T02:00:00+00:00',
    )
    repository.save_measurement_binding(binding)
    stored = repository.get_measurement_binding(binding.binding_id)
    assert stored.measurement_ids == ('m-1', 'm-2')
    assert stored.preset_sha256 == preset.preset_sha256


def test_unknown_scene_revision_is_rejected(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(revision, scene_revision_id='ghost-revision')
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_preset(preset)


def test_summary_is_human_readable_japanese(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='calibration_plan',
                ref_id='plan-1',
                ref_sha256='a' * 64,
                label='初回キャリブレーション',
            ),
            PresetComponentRef(kind='room_operating_state', ref_id='state-9'),
        ),
    )
    summary = operating_mode_summary(preset)
    assert summary.preset_id == preset.preset_id
    assert any('オブジェクトオーディオ' in line for line in summary.lines)
    assert any('初回キャリブレーション' in line for line in summary.lines)
    assert any('部屋の状態' in item for item in summary.unknowns)


def test_hash_integrity_rejects_tampered_preset(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    preset = _preset(revision)
    payload = preset.model_dump(mode='python')
    payload['name'] = 'Tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(preset)(**payload)


def test_scene_component_pin_lives_in_dedicated_fields(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    # 'scene_revision' is not a pinnable component kind — the scene is pinned
    # exclusively by the preset's dedicated scene_revision_id/hash fields.
    with pytest.raises(ValidationError):
        PresetComponentRef(kind='scene_revision', ref_id=revision.revision_id)
    with pytest.raises(ValidationError, match='unique'):
        _preset(
            revision,
            component_refs=(
                PresetComponentRef(kind='calibration_plan', ref_id='p-1'),
                PresetComponentRef(kind='calibration_plan', ref_id='p-1'),
            ),
        )
