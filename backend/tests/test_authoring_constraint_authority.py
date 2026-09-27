"""#843: authoring constraints are versioned design authority.

The singleton payload row is a head pointer only; every save appends a
sealed immutable revision, propagated edits join entity geometry and
constraint state in one Undo step, and corrupt authority fails closed
instead of reading as an empty set.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from htdt.cad_geometric_constraints import (
    AuthoringConstraintSet,
    make_symmetric_pair_constraint,
)
from htdt.cad_repository import (
    AuthoringConstraintConflictError,
    AuthoringConstraintIntegrityError,
    SceneRepository,
)
from htdt.cad_scene import F1_DOCUMENT_ID, Position3, make_f1_scene
from htdt.room_workspace import RoomWorkspaceController


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _controller(tmp_path: Path) -> RoomWorkspaceController:
    return RoomWorkspaceController(_repository(tmp_path), F1_DOCUMENT_ID)


def _corrupt_head(repository: SceneRepository, document_id: str) -> None:
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE authoring_constraint_sets SET payload_json='{not json' "
            "WHERE document_id=?",
            (document_id,),
        )


def test_constraint_saves_append_immutable_revisions(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    revision_id = controller.working.source_revision_id
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-c', 'furniture-left')
    )

    lineage = controller.repository.list_authoring_constraint_revisions(
        F1_DOCUMENT_ID
    )
    assert len(lineage) == 2
    newest, first = lineage
    assert newest.supersedes_id == first.constraint_revision_id
    assert newest.scene_revision_id == revision_id
    assert len(first.payload['constraints']) == 1
    assert len(newest.payload['constraints']) == 2

    # The head resolves to the newest sealed revision; each revision
    # keeps its own immutable payload.
    head = controller.repository.authoring_constraint_head(F1_DOCUMENT_ID)
    assert head is not None
    assert head.constraint_revision_id == newest.constraint_revision_id
    assert head.payload == newest.payload
    stored = controller.repository.authoring_constraints(F1_DOCUMENT_ID)
    assert stored is not None
    assert len(stored.payload['constraints']) == 2


def test_historical_scene_revision_resolves_its_constraints(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    revision_a_id = controller.working.source_revision_id

    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )

    # Later scene revision + later constraint edit under it.
    entity = controller.document.entity('furniture-left')
    controller.working.apply_entity_set_edit(
        replaced_before=(entity,),
        replaced_after=(entity.model_copy(update={'name': 'ソファB'}),),
    )
    controller.save()
    revision_b_id = controller.working.source_revision_id
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-c', 'furniture-left')
    )

    governed_a = repository.authoring_constraints_for_scene(
        F1_DOCUMENT_ID, revision_a_id
    )
    governed_b = repository.authoring_constraints_for_scene(
        F1_DOCUMENT_ID, revision_b_id
    )
    assert governed_a is not None and len(governed_a.payload['constraints']) == 1
    assert governed_b is not None and len(governed_b.payload['constraints']) == 2
    # The constraint bound under revision A keeps its identity.
    assert governed_a.scene_revision_id == revision_a_id


def test_propagated_edit_undoes_geometry_and_constraint_state(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'furniture-left')
    )
    state_before = controller.authoring_constraints

    driver = controller.document.entity('speaker-fl')
    subject = controller.document.entity('furniture-left')
    moved = driver.model_copy(
        update={'position': Position3(x_m=1.0, y_m=0.9, z_m=1.05)}
    )
    controller.working.apply_entity_set_edit(
        replaced_before=(driver,), replaced_after=(moved,)
    )
    notes = controller.propagate_constraints({'speaker-fl'})
    assert notes
    assert controller.authoring_constraints.solve_version == (
        state_before.solve_version + 1
    )

    # One Undo reverts the propagation command: subject position and the
    # constraint-set version come back together.
    assert controller.undo()
    restored = controller.document.entity('furniture-left')
    assert restored == subject
    assert controller.authoring_constraints == state_before

    # Redo restores both again.
    assert controller.redo()
    assert controller.document.entity('furniture-left').position.x_m == (
        pytest.approx(2 * 3.0 - 1.0)
    )
    assert controller.authoring_constraints.solve_version == (
        state_before.solve_version + 1
    )


def test_constraint_add_remove_participate_in_undo(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    assert len(controller.authoring_constraints.constraints) == 1

    assert controller.undo()
    assert controller.authoring_constraints.constraints == ()
    assert controller.redo()
    assert len(controller.authoring_constraints.constraints) == 1

    constraint_id = controller.authoring_constraints.constraints[
        0
    ].constraint_id
    state = controller.authoring_constraints
    controller.update_authoring_constraints(
        state.model_copy(
            update={
                'constraints': tuple(
                    c
                    for c in state.constraints
                    if c.constraint_id != constraint_id
                ),
                'solve_version': state.solve_version + 1,
            }
        )
    )
    assert controller.authoring_constraints.constraints == ()
    assert controller.undo()
    assert len(controller.authoring_constraints.constraints) == 1


def test_constraint_save_rejects_a_head_that_moved(tmp_path: Path) -> None:
    """A stale pre-lock head resolution must not supersede the live head."""
    repository = _repository(tmp_path)
    payload = AuthoringConstraintSet().model_dump(mode='json')
    first = repository.save_authoring_constraints(F1_DOCUMENT_ID, payload)
    second = repository.save_authoring_constraints(F1_DOCUMENT_ID, payload)

    # Simulate a save whose head was resolved before a racing writer
    # committed: the pointer row already names ``second``, so writing a
    # revision superseding the stale ``first`` head must be refused.
    repository.authoring_constraint_head = lambda document_id: first
    with pytest.raises(AuthoringConstraintConflictError):
        repository.save_authoring_constraints(F1_DOCUMENT_ID, payload)
    del repository.authoring_constraint_head

    # The live head is untouched and no third revision was persisted.
    head = repository.authoring_constraint_head(F1_DOCUMENT_ID)
    assert head is not None
    assert head.constraint_revision_id == second.constraint_revision_id
    assert len(
        repository.list_authoring_constraint_revisions(F1_DOCUMENT_ID)
    ) == 2


def test_corrupt_constraint_authority_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    controller._constraint_state = None
    _corrupt_head(repository, F1_DOCUMENT_ID)

    # Reads surface the integrity problem — never an empty set.
    with pytest.raises(AuthoringConstraintIntegrityError):
        controller.authoring_constraints
    with pytest.raises(AuthoringConstraintIntegrityError):
        repository.authoring_constraints(F1_DOCUMENT_ID)
    # And mutation is blocked while corruption stands.
    with pytest.raises(AuthoringConstraintIntegrityError):
        repository.save_authoring_constraints(
            F1_DOCUMENT_ID, AuthoringConstraintSet().model_dump(mode='json')
        )
    # The corrupt row was retained, not overwritten.
    assert repository.repair_authoring_constraints(F1_DOCUMENT_ID)
    repository.save_authoring_constraints(
        F1_DOCUMENT_ID, AuthoringConstraintSet().model_dump(mode='json')
    )
    assert repository.authoring_constraints(F1_DOCUMENT_ID) is not None


def test_deleted_member_relationship_stays_inspectable(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    entity = controller.document.entity('speaker-fr')
    controller.working.apply_entity_set_edit(removed=(entity,))
    broken = controller.mark_broken_constraints()
    assert broken

    lineage = controller.repository.list_authoring_constraint_revisions(
        F1_DOCUMENT_ID
    )
    assert len(lineage) == 2
    # The pre-deletion relation is still inspectable in the lineage.
    original = lineage[1].payload['constraints'][0]
    assert original['broken'] is False
    assert tuple(original['entity_ids']) == ('speaker-fl', 'speaker-fr')
    # The head records the explicit broken state — never silently dropped.
    head = lineage[0].payload['constraints'][0]
    assert head['broken'] is True

    # Undo restores the un-broken relation; the entity itself needs the
    # delete's own Undo step — lineage keeps both versions meanwhile.
    assert controller.undo()
    assert not controller.authoring_constraints.constraints[0].broken


def test_same_name_replacement_never_inherits_relation(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    original = controller.authoring_constraints.constraints[0]

    # Delete and re-add an entity under a new id with the same display name.
    fr = controller.document.entity('speaker-fr')
    controller.working.apply_entity_set_edit(removed=(fr,))
    controller.mark_broken_constraints()
    replacement = fr.model_copy(update={'entity_id': 'speaker-fr-2'})
    controller.working.apply_entity_set_edit(added=(replacement,))

    current = controller.authoring_constraints.constraints[0]
    assert current.constraint_id == original.constraint_id
    assert current.broken
    # No implicit rebinding by name: entity_ids never rewrite themselves.
    assert 'speaker-fr-2' not in current.entity_ids
