from pathlib import Path

import pytest

from htdt.cad_constraint_models import (
    CadConstraintPoint2D,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadWallClearanceConstraint,
)
from htdt.cad_constraint_repository import CadConstraintRepository
from htdt.cad_constraints import (
    CadConstraintAdapterError,
    evaluate_cad_constraints,
    scene_to_g10_context,
    wall_edge_maps,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_walls import make_wall_topology, split_wall


def make_scene() -> SceneDocument:
    room = RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)
    topology = make_wall_topology(room)
    return SceneDocument(
        document_id='n50-fixture',
        schema_version=3,
        room=room,
        wall_topology=topology,
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=0.25, z_m=1.0),
                size_m=Size3(x_m=0.20, y_m=0.20, z_m=0.40),
            ),
            SceneEntity(
                entity_id='seat-main',
                kind='seat',
                name='Seat',
                position=Position3(x_m=3.0, y_m=2.0, z_m=0.45),
                size_m=Size3(x_m=0.70, y_m=0.80, z_m=0.90),
            ),
        ),
    )


def make_constraints() -> CadConstraintSet:
    return CadConstraintSet(
        document_id='n50-fixture',
        constraints=(
            CadWallClearanceConstraint(
                constraint_id='front-clearance',
                name='前壁離隔',
                entity_ids=('speaker-fl',),
                wall_id='wall:front-left->front-right',
                min_m=0.40,
            ),
            CadExclusionRegionConstraint(
                constraint_id='walkway-main',
                name='主通路',
                entity_ids=('seat-main',),
                region_role='walkway',
                vertices=(
                    CadConstraintPoint2D(x_m=2.5, y_m=1.0),
                    CadConstraintPoint2D(x_m=3.5, y_m=1.0),
                    CadConstraintPoint2D(x_m=3.5, y_m=3.0),
                    CadConstraintPoint2D(x_m=2.5, y_m=3.0),
                ),
            ),
        ),
    )


def test_scene_adapter_uses_domain_coordinates_and_exact_source_orientation_profiles() -> None:
    scene = make_scene()
    context = scene_to_g10_context(scene)

    assert context['room']['geometry_kind'] == 'polygon_prism'
    assert context['room']['width_m'] == 6.0
    assert context['room']['depth_m'] == 4.0
    assert context['speakers'][0]['speaker_id'] == 'speaker-fl'
    assert context['speakers'][0]['position'] == {'x_m': 1.0, 'y_m': 0.25, 'z_m': 1.0}

    evaluation = evaluate_cad_constraints(scene, make_constraints())
    wall = next(item for item in evaluation.results if item.constraint_id == 'front-clearance')
    # Source body yaw is 0 degrees, so the exact 0.20 m cabinet depth
    # occupies y +/-0.10 m. The front-wall clearance is therefore 0.15 m.
    assert wall.raw_actual['footprint_mode'] == 'oriented_polygon'
    assert wall.raw_actual['effective_radius_m'] == pytest.approx(2**0.5 * 0.10)
    assert wall.actual_m == pytest.approx(0.15)


def test_wall_mapping_keeps_stable_wall_ids_outside_legacy_edge_contract() -> None:
    scene = make_scene()
    wall_to_edge, edge_to_wall = wall_edge_maps(scene)
    assert wall_to_edge['wall:front-left->front-right'] == 'front-left->front-right'
    assert edge_to_wall['front-left->front-right'] == 'wall:front-left->front-right'

    room, topology = split_wall(
        scene.room,
        scene.wall_topology,
        'wall:front-left->front-right',
        offset_m=3.0,
        new_vertex_id='front-mid',
        first_wall_id='wall:front-left-half',
        second_wall_id='wall:front-right-half',
    )
    split_scene = scene.model_copy(update={'room': room, 'wall_topology': topology})
    wall_to_edge, edge_to_wall = wall_edge_maps(split_scene)
    assert wall_to_edge['wall:front-left-half'] == 'front-left->front-mid'
    assert wall_to_edge['wall:front-right-half'] == 'front-mid->front-right'
    assert edge_to_wall['front-mid->front-right'] == 'wall:front-right-half'


def test_wall_clearance_and_walkway_rejections_map_to_native_reason_and_targets() -> None:
    evaluation = evaluate_cad_constraints(make_scene(), make_constraints())

    assert evaluation.constraints_satisfied is False
    violations = {item.constraint_id: item for item in evaluation.violations}
    assert set(violations) == {'front-clearance', 'walkway-main'}

    wall = violations['front-clearance']
    assert wall.wall_id == 'wall:front-left->front-right'
    assert wall.entity_ids == ('speaker-fl',)
    assert wall.actual_m is not None and wall.actual_m < 0.40
    assert wall.required_min_m == pytest.approx(0.40)
    assert wall.reason_code == 'wall_clearance.out_of_range'
    assert '壁' in wall.reason_ja

    walkway = violations['walkway-main']
    assert walkway.entity_ids == ('seat-main',)
    assert walkway.region_role == 'walkway'
    assert walkway.reason_code == 'exclusion_region.walkway_intersection'
    assert '通路' in walkway.reason_ja


def test_position_override_is_evaluated_without_mutating_scene() -> None:
    scene = make_scene()
    before = scene.entity('speaker-fl')
    moved = Position3(x_m=1.0, y_m=1.0, z_m=1.0)

    evaluation = evaluate_cad_constraints(
        scene,
        make_constraints(),
        position_overrides={'speaker-fl': moved},
    )
    wall = next(item for item in evaluation.results if item.constraint_id == 'front-clearance')
    assert wall.passed is True
    assert scene.entity('speaker-fl') == before


def test_unknown_wall_is_rejected_instead_of_guessed() -> None:
    constraints = make_constraints()
    invalid = constraints.model_copy(update={
        'constraints': (
            constraints.constraints[0].model_copy(update={'wall_id': 'wall:missing'}),
            constraints.constraints[1],
        )
    })
    with pytest.raises(CadConstraintAdapterError, match='unknown wall_id'):
        evaluate_cad_constraints(make_scene(), invalid)


def test_constraint_workspace_round_trips_beside_scene_repository(tmp_path: Path) -> None:
    database = tmp_path / 'cad-scenes.sqlite3'
    scene_repository = SceneRepository(database)
    saved = scene_repository.save(make_scene(), parent_revision_id=None)
    assert saved.created

    constraint_repository = CadConstraintRepository(database)
    expected = make_constraints()
    constraint_repository.save(expected)
    assert constraint_repository.load(expected.document_id) == expected

    reopened_scene = scene_repository.latest(expected.document_id)
    assert reopened_scene is not None
    assert reopened_scene.document == make_scene()
