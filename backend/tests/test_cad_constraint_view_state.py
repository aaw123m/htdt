from htdt.cad_constraint_models import CadConstraintSet, CadWallClearanceConstraint
from htdt.cad_constraints import evaluate_cad_constraints
from htdt.cad_document import EditorViewState
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_walls import make_wall_topology


def test_hide_and_lock_do_not_change_constraint_evaluation() -> None:
    room = RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)
    scene = SceneDocument(
        document_id='view-state-constraint-fixture',
        schema_version=3,
        room=room,
        wall_topology=make_wall_topology(room),
        entities=(
            SceneEntity(
                entity_id='speaker',
                kind='speaker',
                name='Speaker',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.20, y_m=0.20, z_m=0.40),
            ),
        ),
    )
    constraints = CadConstraintSet(
        document_id=scene.document_id,
        constraints=(
            CadWallClearanceConstraint(
                constraint_id='front-clearance',
                name='Front clearance',
                entity_ids=('speaker',),
                wall_id='wall:front-left->front-right',
                min_m=0.30,
            ),
        ),
    )

    before = evaluate_cad_constraints(scene, constraints)
    view_state = EditorViewState()
    view_state.set_hidden('speaker', True)
    view_state.set_locked('speaker', True)
    after = evaluate_cad_constraints(scene, constraints)

    assert view_state.is_hidden('speaker')
    assert view_state.is_locked('speaker')
    assert after == before
