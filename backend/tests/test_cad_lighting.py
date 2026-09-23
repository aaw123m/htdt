"""Theater lighting scene tests (#640)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_lighting import (
    ChannelState,
    LightingAmbientObservation,
    LightingCommissioningRecord,
    LightingFixture,
    LightingZone,
    StageLevel,
    build_lighting_scene,
    evaluate_lighting_scene,
    lighting_scene_status,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='user_defined',
            source_name='installer',
            source_version='1',
            source_reference='commissioning notes',
            source_sha256='7' * 64,
        ),
    )


def _fixtures():
    return (
        LightingFixture(
            fixture_id='fix-bias-1',
            label='bias bar',
            fixture_type='bias_bar',
            control_ref='hue:light-17',
            cct_min_k=1800,
            cct_max_k=6500,
        ),
        LightingFixture(
            fixture_id='fix-down-1',
            fixture_type='downlight',
            controllable_level=True,
        ),
        LightingFixture(
            fixture_id='fix-sconce-1',
            fixture_type='sconce',
        ),
    )


def _zones():
    return (
        LightingZone(
            zone_id='zone-bias',
            role='bias',
            member_fixture_ids=('fix-bias-1',),
            bias_target_entity_id='display-main',
            capability='measured_at_screen',
        ),
        LightingZone(
            zone_id='zone-general',
            role='general',
            member_fixture_ids=('fix-down-1', 'fix-sconce-1'),
        ),
    )


def _scene():
    return build_lighting_scene(
        scene_id='scene-movie',
        version='1',
        label='Movie',
        purpose='viewing',
        states=(
            ChannelState(
                ref_kind='zone', ref_id='zone-bias', level_percent=20.0,
                cct_k=6500,
            ),
            ChannelState(
                ref_kind='zone', ref_id='zone-general', level_percent=0.0
            ),
        ),
        provenance=_provenance(),
    )


def _ambient_for(scene):
    return LightingAmbientObservation(
        observation_id='obs-1',
        scene_id=scene.scene_id,
        scene_version=scene.version,
        scene_sha256=scene.scene_sha256,
        location_label='screen wall',
        plane='screen',
        measured_lux=4.5,
        instrument='lux-meter-1',
        measured_at_utc='2026-09-23T02:00:00+00:00',
        room_operating_state_id='ros-movie',
    )


def test_bias_zone_requires_target():
    with pytest.raises(ValueError, match='bias zones must'):
        LightingZone(
            zone_id='z', role='bias', member_fixture_ids=('f',)
        )
    with pytest.raises(ValueError, match='only valid on bias'):
        LightingZone(
            zone_id='z',
            role='general',
            member_fixture_ids=('f',),
            bias_target_entity_id='display-main',
        )


def test_scene_rejects_duplicate_refs():
    with pytest.raises(ValueError, match='same fixture/zone twice'):
        build_lighting_scene(
            scene_id='s',
            version='1',
            label='x',
            states=(
                ChannelState(
                    ref_kind='fixture', ref_id='f1', level_percent=50.0
                ),
                ChannelState(
                    ref_kind='fixture', ref_id='f1', level_percent=60.0
                ),
            ),
        )


def test_scene_hash_integrity():
    payload = _scene().model_dump(mode='python')
    payload['label'] = 'Different'
    from htdt.cad_lighting import LightingScene
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        LightingScene(**payload)


def test_evaluation_resolves_and_ambient():
    scene = _scene()
    evaluation = evaluate_lighting_scene(
        scene=scene,
        fixtures=_fixtures(),
        zones=_zones(),
        ambient_observation=_ambient_for(scene),
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['references_resolve'] == 'PASS'
    assert checks['bias_target_bound'] == 'PASS'
    assert checks['ambient_evidence'] == 'PASS'
    # never claim photometric modeling
    assert checks['photometric_modeling'] == 'UNKNOWN'
    assert evaluation.evaluation_id.startswith('lse-')


def test_evaluation_flags_unresolved_ref():
    scene = build_lighting_scene(
        scene_id='scene-x',
        version='1',
        label='x',
        states=(
            ChannelState(
                ref_kind='zone', ref_id='zone-missing', level_percent=10.0
            ),
        ),
    )
    evaluation = evaluate_lighting_scene(
        scene=scene, fixtures=_fixtures(), zones=_zones()
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['references_resolve'] == 'FAIL'
    assert checks['ambient_evidence'] == 'UNKNOWN'
    assert lighting_scene_status(evaluation) == 'FAIL'


def test_ambient_bound_to_exact_scene_hash():
    scene = _scene()
    stale = LightingAmbientObservation(
        observation_id='obs-stale',
        scene_id=scene.scene_id,
        scene_version=scene.version,
        scene_sha256='0' * 64,  # bound to a different scene content
        location_label='screen wall',
        measured_lux=4.0,
        measured_at_utc='2026-09-23T02:00:00+00:00',
    )
    evaluation = evaluate_lighting_scene(
        scene=scene,
        fixtures=_fixtures(),
        zones=_zones(),
        ambient_observation=stale,
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['ambient_evidence'] == 'FAIL'


def test_setpoint_stages_stay_separate():
    scene = _scene()
    record = LightingCommissioningRecord(
        record_id='rec-1',
        scene_id=scene.scene_id,
        scene_version=scene.version,
        scene_sha256=scene.scene_sha256,
        ref_kind='zone',
        ref_id='zone-bias',
        desired=StageLevel(stage='desired', level_percent=20.0),
        commanded=StageLevel(stage='commanded', level_percent=20.0),
        read_back=StageLevel(
            stage='read_back', level_percent=19.0, source='hue bridge'
        ),
        measured=StageLevel(
            stage='measured',
            level_percent=18.0,
            observed_at_utc='2026-09-23T02:05:00+00:00',
        ),
    )
    assert record.desired.level_percent == 20.0
    assert record.measured.level_percent == 18.0
    # no field fuses or derives another
    assert record.commanded is not record.read_back
