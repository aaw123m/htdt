"""Round 14 review pins: entity-hierarchy / attachment transform truth (#661).

Composed-transform math is verified numerically — never trusting intermediate
values — plus delete/detach landing, undo chains, preview honesty, doc-level
fail-closed validation, and diff truthfulness.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_attachment_models import AssemblyLayer, ConstructionAssembly, EntityAttachment
from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository
from htdt.cad_room import RoomWorkingDocument
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_scene_history import diff_scene_documents, diff_summary_lines
from htdt.cad_walls import make_wall_topology, split_wall
from htdt.physical_attachment import (
    attachment_graph,
    attached_world_position,
    resolve_attached_positions,
)


def _entity(
    entity_id: str, *, x: float, y: float, z: float,
    size=(0.5, 0.5, 0.5), kind: str = 'furniture', **extra,
) -> SceneEntity:
    if kind == 'speaker':
        extra.setdefault('speaker_role', 'FL')
    return SceneEntity(
        entity_id=entity_id,
        kind=kind,
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=z),
        size_m=Size3(x_m=size[0], y_m=size[1], z_m=size[2]),
        **extra,
    )


def _document(*entities: SceneEntity, attachments=(), assemblies=(), topology=None) -> SceneDocument:
    return SceneDocument(
        document_id='rig-fixture',
        schema_version=5,
        room=RoomPrism(width_m=10.0, depth_m=8.0, height_m=3.0),
        wall_topology=topology,
        entities=entities,
        attachments=attachments or None,
        construction_assemblies=assemblies or None,
    )


def _stand_scene() -> SceneDocument:
    return _document(
        _entity('stand', x=2.0, y=2.0, z=0.5, size=(0.6, 0.6, 1.0)),
        # spk already sits at its derived pose (stand top surface).
        _entity('spk', x=2.0, y=2.0, z=1.0, size=(0.3, 0.3, 0.4), kind='speaker'),
        attachments=(
            EntityAttachment(
                attachment_id='spk-on-stand', child_entity_id='spk',
                parent_entity_id='stand', kind='stand_on',
                parent_anchor='top_surface',
            ),
        ),
    )


# --- composed transform ------------------------------------------------------

def test_front_face_mount_composes_parent_yaw() -> None:
    """Parent yaw 90°: front_face local (0,-hy,0) must rotate to world (+hy,0)."""

    rack = _entity('rack', x=8.0, y=2.0, z=0.8, size=(0.6, 0.5, 1.6),
                   orientation=quaternion_from_euler_deg(yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0))
    display = _entity('disp', x=0.0, y=0.0, z=0.0, size=(0.4, 0.1, 0.3))
    document = _document(
        rack, display,
        attachments=(
            EntityAttachment(
                attachment_id='d-on-r', child_entity_id='disp',
                parent_entity_id='rack', kind='mounted_to',
                parent_anchor='front_face',
            ),
        ),
    )
    pos = attached_world_position(
        document, attachment_graph(document)['disp'],
    )
    # front_face local offset (0,-0.25,0) under yaw+90° maps to (+0.25,0,0).
    assert pos.x_m == pytest.approx(8.25, abs=1e-9)
    assert pos.y_m == pytest.approx(2.0, abs=1e-9)
    assert pos.z_m == pytest.approx(0.8, abs=1e-9)


def test_child_offset_rotates_with_parent() -> None:
    rack = _entity('rack', x=8.0, y=2.0, z=0.8, size=(0.6, 0.5, 1.6),
                   orientation=quaternion_from_euler_deg(yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0))
    display = _entity('disp', x=0.0, y=0.0, z=0.0, size=(0.4, 0.1, 0.3))
    document = _document(
        rack, display,
        attachments=(
            EntityAttachment(
                attachment_id='d-on-r', child_entity_id='disp',
                parent_entity_id='rack', kind='mounted_to',
                parent_anchor='front_face',
                child_anchor_offset_m=(0.1, 0.0, 0.0),
            ),
        ),
    )
    pos = attached_world_position(document, attachment_graph(document)['disp'])
    # local (0,-0.25,0)+(0.1,0,0) rotated yaw+90° → (+0.25,+0.1,0)
    assert pos.x_m == pytest.approx(8.25, abs=1e-9)
    assert pos.y_m == pytest.approx(2.1, abs=1e-9)
    assert pos.z_m == pytest.approx(0.8, abs=1e-9)


def test_identity_orientation_unchanged() -> None:
    """Identity parent orientation keeps the previous axis-aligned answer."""

    document = _stand_scene()
    pos = attached_world_position(document, attachment_graph(document)['spk'])
    assert pos.x_m == pytest.approx(2.0)
    assert pos.y_m == pytest.approx(2.0)
    assert pos.z_m == pytest.approx(1.0)  # stand top at z=0.5+0.5


# --- move/rotate parent: children follow -------------------------------------

def test_move_parent_carries_child_via_history() -> None:
    working = WorkingDocument(_stand_scene())
    assert working.move_entity('stand', Position3(x_m=5.0, y_m=3.0, z_m=0.5))
    child = working.document.entity('spk')
    # derived = parent pos + top-surface local (0,0,+0.5)
    assert child.position.x_m == pytest.approx(5.0)
    assert child.position.y_m == pytest.approx(3.0)
    assert child.position.z_m == pytest.approx(1.0)


def test_stale_cached_child_position_heals_on_next_edit() -> None:
    """A stored child position that drifted re-derives at the next history op."""

    stale = _stand_scene().model_copy(update={'entities': tuple(
        entity.model_copy(update={'position': Position3(x_m=9.9, y_m=9.9, z_m=0.1)})
        if entity.entity_id == 'spk' else entity
        for entity in _stand_scene().entities
    )})
    working = WorkingDocument(stale)
    working.move_entity('stand', Position3(x_m=2.5, y_m=2.0, z_m=0.5))
    child = working.document.entity('spk')
    assert child.position.x_m == pytest.approx(2.5)
    assert child.position.z_m == pytest.approx(1.0)


def test_rotate_parent_moves_child_anchor() -> None:
    rack = _entity('rack', x=8.0, y=2.0, z=0.8, size=(0.6, 0.5, 1.6))
    display = _entity('disp', x=0.0, y=0.0, z=0.0, size=(0.4, 0.1, 0.3))
    document = _document(
        rack, display,
        attachments=(
            EntityAttachment(
                attachment_id='d-on-r', child_entity_id='disp',
                parent_entity_id='rack', kind='mounted_to',
                parent_anchor='front_face',
            ),
        ),
    )
    working = WorkingDocument(document)
    assert working.rotate_entity(
        'rack',
        quaternion_from_euler_deg(yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0),
    )
    child = working.document.entity('disp')
    assert child.position.x_m == pytest.approx(8.25, abs=1e-9)
    assert child.position.y_m == pytest.approx(2.0, abs=1e-9)


def test_preview_shows_children_following_parent() -> None:
    working = WorkingDocument(_stand_scene())
    working.begin_move('stand')
    working.preview_move(Position3(x_m=5.0, y_m=3.0, z_m=0.5))
    child = working.document.entity('spk')
    assert child.position.x_m == pytest.approx(5.0)
    assert child.position.z_m == pytest.approx(1.0)
    assert working.commit_preview()
    assert working.document.entity('spk').position.x_m == pytest.approx(5.0)


# --- delete parent: detach honestly ------------------------------------------

def test_delete_parent_lands_orphan_at_derived_world_pose() -> None:
    working = WorkingDocument(_stand_scene())
    before_child = working.document.entity('spk').position
    assert working.delete_entities(('stand',))
    doc = working.document
    assert doc.attachments is None or not doc.attachments
    orphan = doc.entity('spk')
    assert orphan.position.x_m == pytest.approx(before_child.x_m)
    assert orphan.position.y_m == pytest.approx(before_child.y_m)
    assert orphan.position.z_m == pytest.approx(before_child.z_m)
    # The surviving document must re-validate — no dangling parent refs.
    SceneDocument.model_validate(doc.model_dump(mode='python'))


def test_delete_parent_undo_restores_relationship_exactly() -> None:
    working = WorkingDocument(_stand_scene())
    before = working.document
    working.delete_entities(('stand',))
    assert working.undo()
    restored = working.document
    assert restored.attachments == before.attachments
    assert restored.entity('stand').position == before.entity('stand').position
    assert restored.entity('spk').position == before.entity('spk').position


def test_delete_parent_and_child_removes_edge() -> None:
    working = WorkingDocument(_stand_scene())
    assert working.delete_entities(('stand', 'spk'))
    assert working.document.attachments is None
    assert not working.document.entities


def test_detached_child_stays_free_after_orphan_landing() -> None:
    working = WorkingDocument(_stand_scene())
    working.delete_entities(('stand',))
    # The orphan now owns a free world position — a subsequent move sticks.
    working.move_entity('spk', Position3(x_m=7.0, y_m=7.0, z_m=0.6))
    assert working.document.entity('spk').position.x_m == pytest.approx(7.0)


def test_undo_chain_move_then_delete_restores_all() -> None:
    working = WorkingDocument(_stand_scene())
    original = working.document
    working.move_entity('stand', Position3(x_m=5.0, y_m=3.0, z_m=0.5))
    working.delete_entities(('stand',))
    assert working.undo()  # delete undone → edge back, child re-attached
    doc = working.document
    assert doc.attachments == original.attachments
    # child re-derives under the moved parent
    assert doc.entity('spk').position.x_m == pytest.approx(5.0)
    assert working.undo()  # move undone → exact original
    assert working.document.entity('stand').position == original.entity('stand').position
    assert working.document.entity('spk').position == original.entity('spk').position
    assert working.document.attachments == original.attachments


# --- persistence --------------------------------------------------------------

def test_save_reload_keeps_graph_and_derived_positions(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    working = WorkingDocument(_stand_scene())
    working.move_entity('stand', Position3(x_m=5.0, y_m=3.0, z_m=0.5))
    repository.save(working.document, parent_revision_id=None)
    reopened = repository.latest('rig-fixture')
    assert reopened is not None
    assert reopened.document.attachments == working.document.attachments
    assert reopened.document.entity('spk').position == working.document.entity('spk').position


# --- diff truthfulness --------------------------------------------------------

def test_scene_diff_reports_attachment_changes() -> None:
    before = _stand_scene()
    after = before.model_copy(update={
        'attachments': (
            EntityAttachment(
                attachment_id='spk-on-stand', child_entity_id='spk',
                parent_entity_id='stand', kind='stand_on',
                parent_anchor='top_surface',
                child_anchor_offset_m=(0.05, 0.0, 0.0),
            ),
        ),
    })
    diff = diff_scene_documents(before, after)
    assert diff.attachments_changed
    lines = diff_summary_lines(diff, after)
    assert any('取付' in line for line in lines)
    unchanged = diff_scene_documents(before, before)
    assert not unchanged.attachments_changed
    assert unchanged.is_empty


# --- fail-closed validation ---------------------------------------------------

def test_document_rejects_cycle_at_construction() -> None:
    a = _entity('a', x=1, y=1, z=0.5)
    b = _entity('b', x=2, y=1, z=0.5)
    with pytest.raises(ValidationError, match='cycle'):
        _document(
            a, b,
            attachments=(
                EntityAttachment(
                    attachment_id='ab', child_entity_id='a', parent_entity_id='b',
                    kind='stand_on', parent_anchor='top_surface',
                ),
                EntityAttachment(
                    attachment_id='ba', child_entity_id='b', parent_entity_id='a',
                    kind='stand_on', parent_anchor='top_surface',
                ),
            ),
        )


def test_wall_split_orphaning_assembly_fails_closed() -> None:
    room = RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)
    topology = make_wall_topology(room)
    wall = topology.walls[0]
    assembly = ConstructionAssembly(
        assembly_id='asm-w0', element='wall', element_ref=wall.wall_id,
        layers=(AssemblyLayer(layer_id='f', kind='finish', material='drywall',
                              thickness_m=0.0125),),
    )
    document = SceneDocument(
        document_id='rig-fixture', schema_version=5, room=room,
        wall_topology=topology, entities=(),
        construction_assemblies=(assembly,),
    )
    new_room, new_topology = split_wall(
        room, topology, wall.wall_id, offset_m=3.0,
        new_vertex_id='v-mid', first_wall_id='w0a', second_wall_id='w0b',
    )
    working = RoomWorkingDocument(document)
    with pytest.raises(ValueError, match='unknown wall'):
        working.replace_room_topology(new_room, new_topology)
