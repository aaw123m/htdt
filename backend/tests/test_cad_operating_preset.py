from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_measurement_disposition import build_measurement_disposition
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
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
from htdt.cad_operating_preset_repository import (
    CadOperatingPresetRepository,
    PresetComponentResolution,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_room_operating_state import build_operating_state
from htdt.cad_room_operating_state_repository import (
    CadRoomOperatingStateRepository,
)
from htdt.cad_scene import make_f1_scene

NOW = '2026-09-23T00:00:00+00:00'
AFTER = '2026-09-23T12:31:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadOperatingPresetRepository(scene_repository)
    return scene_repository, revision, repository


def _save_measurement(
    measurement_repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    captured_at: str | None = None,
    imported_at: str = AFTER,
):
    processing = {'fixture_raw': f'raw-{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        captured_at=captured_at,
        imported_at=imported_at,
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return record


def _save_operating_state(scene_repository, revision):
    states = CadRoomOperatingStateRepository(scene_repository)
    state = build_operating_state(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        name='Night curtains',
        observed_at_utc=NOW,
    )
    states.save_state(state)
    return state


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
    scenes, revision, repository = _repositories(tmp_path)
    state = _save_operating_state(scenes, revision)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='room_operating_state',
                ref_id=state.state_id,
                ref_sha256=state.semantic_sha256,
                label='夜間カーテン',
            ),
            PresetComponentRef(
                kind='other',
                ref_id='svc-dsp-snapshot-7',
                external_dependency=True,
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
                external_dependency=True,
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
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    _save_measurement(measurements, revision, 'm-1')
    _save_measurement(measurements, revision, 'm-2')
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-1', 'm-2'),
        bound_at_utc='2026-09-23T14:00:00+00:00',
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


# --- #743: preset component refs + measurement bindings are validated ------


def test_preset_rejects_unresolvable_component_ref(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='calibration_plan',
                ref_id='plan-ghost',
                ref_sha256='a' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match='does not resolve'):
        repository.save_preset(preset)


def test_preset_component_needs_external_marker_without_resolver(
    tmp_path: Path,
) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(kind='playback_chain', ref_id='chain-1'),
        ),
    )
    with pytest.raises(ValueError, match='external_dependency'):
        repository.save_preset(preset)

    # The explicit unresolved/external contract is allowed.
    preset_ok = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='playback_chain',
                ref_id='chain-1',
                external_dependency=True,
            ),
        ),
    )
    repository.save_preset(preset_ok)


def test_preset_rejects_missing_or_wrong_semantic_pin(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    state = _save_operating_state(scenes, revision)
    # A hash-bearing authority cannot be pinned by bare id alone.
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='room_operating_state', ref_id=state.state_id
            ),
        ),
    )
    with pytest.raises(ValueError, match='semantic hash'):
        repository.save_preset(preset)
    # Nor by a hash that does not match the resolved authority.
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='room_operating_state',
                ref_id=state.state_id,
                ref_sha256='b' * 64,
            ),
        ),
    )
    with pytest.raises(ValueError, match='semantic hash'):
        repository.save_preset(preset)


def test_preset_rejects_foreign_document_component(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    other_revision = scenes.save(
        make_f1_scene().model_copy(update={'document_id': 'doc-2'}),
        parent_revision_id=None,
    ).revision
    foreign = _save_operating_state(scenes, other_revision)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='room_operating_state',
                ref_id=foreign.state_id,
                ref_sha256=foreign.semantic_sha256,
            ),
        ),
    )
    with pytest.raises(ValueError, match='another document'):
        repository.save_preset(preset)


def test_preset_rejects_component_from_other_scene_revision(
    tmp_path: Path,
) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    later_revision = scenes.save(
        make_f1_scene().model_copy(
            update={
                'room': make_f1_scene().room.model_copy(
                    update={'width_m': 5.5}
                )
            }
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    other_state = _save_operating_state(scenes, later_revision)
    preset = _preset(
        revision,
        component_refs=(
            PresetComponentRef(
                kind='room_operating_state',
                ref_id=other_state.state_id,
                ref_sha256=other_state.semantic_sha256,
            ),
        ),
    )
    with pytest.raises(ValueError, match='SceneRevision'):
        repository.save_preset(preset)


def test_preset_component_resolvers_are_injectable(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadOperatingPresetRepository(
        scene_repository,
        component_resolvers={
            'calibration_plan': lambda ref: PresetComponentResolution(
                resolved_id=ref.ref_id,
                document_id=revision.document_id,
                semantic_sha256='a' * 64,
            ),
        },
    )
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
    assert repository.get_preset(preset.preset_id) == preset


def test_binding_rejects_unknown_measurement(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('ghost-m',),
        bound_at_utc=AFTER,
    )
    with pytest.raises(ValueError, match='unknown measurement'):
        repository.save_measurement_binding(binding)


def test_binding_rejects_foreign_document_measurement(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    other_revision = scenes.save(
        make_f1_scene().model_copy(update={'document_id': 'doc-2'}),
        parent_revision_id=None,
    ).revision
    _save_measurement(measurements, other_revision, 'm-foreign')
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-foreign',),
        bound_at_utc=AFTER,
    )
    with pytest.raises(ValueError, match='another document'):
        repository.save_measurement_binding(binding)


def test_binding_rejects_measurement_on_other_scene_revision(
    tmp_path: Path,
) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    later_revision = scenes.save(
        make_f1_scene().model_copy(
            update={
                'room': make_f1_scene().room.model_copy(
                    update={'width_m': 5.5}
                )
            }
        ),
        parent_revision_id=revision.revision_id,
    ).revision
    _save_measurement(measurements, later_revision, 'm-later')
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-later',),
        bound_at_utc=AFTER,
    )
    with pytest.raises(ValueError, match='different SceneRevision'):
        repository.save_measurement_binding(binding)


def test_binding_rejects_ineligible_disposition(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    quality = CadMeasurementQualityRepository(measurements)
    _save_measurement(measurements, revision, 'm-excluded')
    quality.save_disposition(
        build_measurement_disposition(
            document_id=revision.document_id,
            measurement_id='m-excluded',
            disposition='excluded_from_normal_use',
            reason='bad cabling during capture',
            created_at_utc=AFTER,
        )
    )
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-excluded',),
        bound_at_utc=AFTER,
    )
    with pytest.raises(ValueError, match='not eligible'):
        repository.save_measurement_binding(binding)


def test_binding_rejects_predating_measurement_without_attestation(
    tmp_path: Path,
) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    # Imported before the preset's created_at_utc (NOW).
    _save_measurement(
        measurements, revision, 'm-old', imported_at='2026-09-19T12:31:00+00:00'
    )
    preset = _preset(revision)
    repository.save_preset(preset)
    binding = bind_preset_measurements(
        preset,
        measurement_ids=('m-old',),
        bound_at_utc=AFTER,
    )
    with pytest.raises(ValueError, match='historical_attestation'):
        repository.save_measurement_binding(binding)
    # The explicit historical-attestation semantics allow it.
    ok = bind_preset_measurements(
        preset,
        measurement_ids=('m-old',),
        bound_at_utc=AFTER,
        historical_attestation=True,
    )
    repository.save_measurement_binding(ok)


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
            PresetComponentRef(
                kind='room_operating_state',
                ref_id='state-9',
                external_dependency=True,
            ),
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
