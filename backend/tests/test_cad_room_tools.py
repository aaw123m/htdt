"""Backend tests for the room CAD batch (#534/#545/#546/#613/#618/#629).

All verification runs against the controller + domain modules; Qt widgets are
only touched where an offscreen QApplication exists already in this suite.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from htdt.cad_geometric_constraints import (
    AuthoringConstraintSet,
    make_centerline_constraint,
    make_equal_spacing_constraint,
    make_fixed_distance_constraint,
    make_symmetric_pair_constraint,
    solve_constraint,
)
from htdt.cad_layout_tools import (
    LayoutClipboard,
    LayoutError,
    align_entities,
    copy_selection,
    distribute_entities,
    mirror_entities_x,
    mirror_speaker_pair,
    paste_clipboard,
    propose_pair_role,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.cad_seating import (
    AisleSpec,
    SeatRowSpec,
    SeatingLayoutError,
    SeatingLayoutSpec,
    apply_seating_layout,
    plan_regeneration,
    plan_seating,
    spec_owns_entity,
)
from htdt.cad_view_state import (
    ORTHOGRAPHIC_VIEWS,
    PersistedViewState,
    NamedViewSpec,
    RoomCameraState,
    SectionPlaneState,
    STANDARD_VIEW_GEOMETRY,
    StandardView,
)
from htdt.room_underlay import (
    FloorPlanUnderlay,
    UnderlaySourceFormat,
    calibrate_two_point,
    domain_to_source,
    is_calibrated,
    new_underlay_id,
    parse_dxf,
    source_to_domain,
    underlay_snap_points,
    utc_now_iso,
)
from htdt.room_workspace import RoomWorkspaceController


def _repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _controller(tmp_path) -> RoomWorkspaceController:
    return RoomWorkspaceController(_repository(tmp_path), F1_DOCUMENT_ID)


CENTERLINE_X = 3.0  # F1 room is 6m wide (x: 0..6)


# --- #545 / #629: view state -------------------------------------------------


def test_camera_state_roundtrips_through_repository(tmp_path) -> None:
    controller = _controller(tmp_path)
    camera = RoomCameraState(
        position=(1.0, -2.0, 3.0),
        focal_point=(3.0, 2.0, 0.5),
        view_up=(0.0, 0.0, 1.0),
        projection='parallel',
        parallel_scale=2.5,
        view_angle=30.0,
        standard_view=StandardView.TOP.value,
    )
    controller.persist_view_extras(
        PersistedViewState(camera=camera, section=None)
    )
    stored = controller.persisted_view_state()
    assert stored is not None
    assert stored.camera == camera
    assert stored.section is None


def test_named_view_save_recall_delete(tmp_path) -> None:
    controller = _controller(tmp_path)
    camera = RoomCameraState(
        position=(0.0, -1.0, 2.0),
        focal_point=(3.0, 2.0, 0.0),
        view_up=(0.0, 0.0, 1.0),
        projection='perspective',
        parallel_scale=None,
        view_angle=45.0,
        standard_view=StandardView.PERSPECTIVE.value,
    )
    view_id = controller.save_named_view(
        NamedViewSpec(
            name='鳥瞰図',
            camera=camera,
            hidden_ids=('furniture-left',),
            focus_entity_id='speaker-fl',
            section=SectionPlaneState(
                enabled=True, origin=(3.0, 2.0, 1.2), normal=(0.0, 0.0, 1.0)
            ),
        )
    )
    views = controller.named_views()
    assert len(views) == 1
    recalled_id, spec = views[0]
    assert recalled_id == view_id
    assert spec.name == '鳥瞰図'
    assert spec.hidden_ids == ('furniture-left',)
    assert spec.focus_entity_id == 'speaker-fl'
    assert spec.section is not None and spec.section.enabled
    controller.delete_named_view(view_id)
    assert controller.named_views() == ()


def test_standard_view_geometry_is_scene_axis_aligned() -> None:
    assert set(ORTHOGRAPHIC_VIEWS) == {
        StandardView.TOP,
        StandardView.FRONT,
        StandardView.REAR,
        StandardView.LEFT,
        StandardView.RIGHT,
    }
    for view in ORTHOGRAPHIC_VIEWS:
        direction, up = STANDARD_VIEW_GEOMETRY[view]
        assert len(direction) == 3 and len(up) == 3
        assert any(abs(c) > 0.5 for c in direction)
    # Top view looks down -Z with the front wall (min Y) toward screen top.
    top_dir, top_up = STANDARD_VIEW_GEOMETRY[StandardView.TOP]
    assert top_dir == (0.0, 0.0, -1.0)
    assert top_up == (0.0, -1.0, 0.0)


# --- #534: floor-plan underlay ------------------------------------------------


_DXF = b"""0
SECTION
2
ENTITIES
0
LINE
10
0.0
20
0.0
11
4000.0
21
0.0
0
LWPOLYLINE
70
1
10
100.0
20
200.0
10
500.0
20
200.0
0
POINT
10
42.0
20
24.0
0
ENDSEC
0
EOF
"""


def test_parse_dxf_lines_polylines_points() -> None:
    parsed = parse_dxf(_DXF)
    assert len(parsed.segments) >= 2  # LINE + closed LWPOLYLINE edges
    assert (42.0, 24.0) in parsed.points


def test_underlay_calibration_unlocks_snap_hints() -> None:
    underlay = FloorPlanUnderlay(
        underlay_id=new_underlay_id(),
        name='plan',
        source_format=UnderlaySourceFormat.IMAGE,
        source_file_name='plan.png',
        imported_at_utc=utc_now_iso(),
        source_blob_sha256='0' * 64,
        source_width=1000.0,
        source_height=800.0,
        snap_hints=((10.0, 10.0),),
    )
    assert not is_calibrated(underlay)
    # Uncalibrated underlays never emit snap hints.
    assert underlay_snap_points(underlay) == ()
    calibrated = calibrate_two_point(underlay, (0.0, 0.0), (4000.0, 0.0), 4.0)
    assert is_calibrated(calibrated)
    assert calibrated.units_per_meter == pytest.approx(1000.0)
    hints = underlay_snap_points(calibrated)
    assert len(hints) >= 1


def test_underlay_source_domain_roundtrip() -> None:
    underlay = FloorPlanUnderlay(
        underlay_id=new_underlay_id(),
        name='plan',
        source_format=UnderlaySourceFormat.IMAGE,
        imported_at_utc=utc_now_iso(),
        source_blob_sha256='0' * 64,
        units_per_meter=100.0,
        origin_x_m=1.0,
        origin_y_m=2.0,
        rotation_deg=0.0,
    )
    x, y = source_to_domain(underlay, 50.0, 100.0)
    assert x == pytest.approx(1.5)
    assert y == pytest.approx(3.0)
    u, v = domain_to_source(underlay, x, y)
    assert u == pytest.approx(50.0)
    assert v == pytest.approx(100.0)


def test_underlay_import_persists_record_and_blob(tmp_path) -> None:
    pytest.importorskip('PySide6.QtGui')
    import numpy as np
    from PySide6.QtGui import QImage

    image = QImage(8, 4, QImage.Format.Format_RGBA8888)
    image.fill(0xFF112233)
    image_path = tmp_path / 'plan.png'
    assert image.save(str(image_path))

    controller = _controller(tmp_path)
    underlay = controller.import_underlay(str(image_path))
    assert underlay.source_format is UnderlaySourceFormat.IMAGE
    assert underlay.source_width == pytest.approx(8.0)
    assert underlay.source_height == pytest.approx(4.0)
    # Provenance: original bytes are blob-stored and re-readable.
    assert controller.repository.read_blob(underlay.source_blob_sha256) is not None
    # The record persists and the underlay renders.
    assert controller.underlays() == (underlay,)
    items = controller.underlay_render_items()
    assert len(items) == 1
    assert items[0].image is not None and isinstance(items[0].image, np.ndarray)


def test_underlay_never_touches_scene_revision(tmp_path) -> None:
    controller = _controller(tmp_path)
    before = controller.repository.current_head(F1_DOCUMENT_ID)
    dxf_path = tmp_path / 'plan.dxf'
    dxf_path.write_bytes(_DXF)
    controller.import_underlay(str(dxf_path))
    after = controller.repository.current_head(F1_DOCUMENT_ID)
    assert before is not None and after is not None
    assert before.revision_id == after.revision_id


# --- #613: layout tools --------------------------------------------------------


def test_copy_paste_produces_new_ids_and_offset(tmp_path) -> None:
    controller = _controller(tmp_path)
    clipboard = copy_selection(
        controller.document, ('speaker-fl', 'speaker-c')
    )
    assert isinstance(clipboard, LayoutClipboard)
    assert len(clipboard.entities) == 2
    new_ids = paste_clipboard(controller.working, clipboard)
    assert len(new_ids) == 2
    assert not set(new_ids) & {'speaker-fl', 'speaker-c'}
    pasted = controller.document.entity(new_ids[0])
    original = controller.document.entity('speaker-fl')
    assert pasted.position.x_m == pytest.approx(original.position.x_m + 0.25)
    # One atomic undo restores both.
    assert controller.working.undo()
    assert all(
        entity_id not in {e.entity_id for e in controller.document.entities}
        for entity_id in new_ids
    )


def test_mirror_x_mirrors_position_aim_not_role(tmp_path) -> None:
    controller = _controller(tmp_path)
    # FR has an explicit aim direction; mirroring negates its x component.
    before = controller.document.entity('speaker-fr')
    ids = mirror_entities_x(
        controller.working, controller.document, ('speaker-fr',)
    )
    mirrored = controller.document.entity('speaker-fr')
    assert ids == ('speaker-fr',)
    assert mirrored.position.x_m == pytest.approx(
        2 * CENTERLINE_X - before.position.x_m
    )
    assert mirrored.position.y_m == pytest.approx(before.position.y_m)
    assert mirrored.aim_xyz is not None
    assert mirrored.aim_xyz.x == pytest.approx(-before.aim_xyz.x)
    # Roles are never inferred from position.
    assert mirrored.speaker_role == 'FR'


def test_align_and_distribute_are_atomic(tmp_path) -> None:
    controller = _controller(tmp_path)
    moved = align_entities(
        controller.working,
        controller.document,
        ('speaker-fl', 'speaker-c', 'speaker-fr'),
        axis='y',
        mode='min',
    )
    # All min-edges coincide at speaker-c's front edge (0.55 - 0.28/2 = 0.41):
    # centers differ by size, edges are equal.
    fl = controller.document.entity('speaker-fl')
    assert fl.position.y_m - fl.size_m.y_m / 2 == pytest.approx(0.41)
    assert len(moved) == 3
    assert controller.working.undo()
    assert controller.document.entity('speaker-fl').position.y_m == pytest.approx(0.75)

    # Knock C off the midpoint so distribute has something to move.
    c = controller.document.entity('speaker-c')
    moved_c = c.model_copy(
        update={'position': Position3(x_m=3.5, y_m=c.position.y_m, z_m=c.position.z_m)}
    )
    controller.working.apply_entity_set_edit(
        replaced_before=(c,), replaced_after=(moved_c,)
    )
    moved = distribute_entities(
        controller.working,
        controller.document,
        ('speaker-fl', 'speaker-c', 'speaker-fr'),
        axis='x',
    )
    assert set(moved) == {'speaker-fl', 'speaker-c', 'speaker-fr'}
    xs = sorted(
        controller.document.entity(entity_id).position.x_m
        for entity_id in ('speaker-fl', 'speaker-c', 'speaker-fr')
    )
    # Endpoints fixed at 1.35 / 4.65; middle lands exactly halfway.
    assert xs[1] - xs[0] == pytest.approx(xs[2] - xs[1])
    assert xs[0] == pytest.approx(1.35) and xs[2] == pytest.approx(4.65)


def test_pair_role_proposal_is_canonical_and_bounded() -> None:
    assert propose_pair_role('FL') == 'FR'
    assert propose_pair_role('FR') == 'FL'
    assert propose_pair_role('C') is None
    assert propose_pair_role('BOGUS') is None


def test_mirror_speaker_pair_keeps_role_unless_accepted(tmp_path) -> None:
    controller = _controller(tmp_path)
    new_id = mirror_speaker_pair(
        controller.working,
        controller.document,
        'speaker-fl',
        apply_role_proposal=False,
    )
    assert controller.document.entity(new_id).speaker_role == 'FL'
    new_id2 = mirror_speaker_pair(
        controller.working,
        controller.document,
        'speaker-c',
        apply_role_proposal=True,
    )
    # 'C' has no canonical pair -> role stays verbatim.
    assert controller.document.entity(new_id2).speaker_role == 'C'
    # Undo removes just the last paste.
    assert controller.working.undo()
    with pytest.raises(KeyError):
        controller.document.entity(new_id2)


# --- #546: seating layout -------------------------------------------------------


def _seat_spec(**overrides) -> SeatingLayoutSpec:
    params = dict(
        spec_id='seatlayout-test01',
        name='テスト座席',
        anchor_x_m=1.0,
        anchor_y_m=2.0,
        rows=(
            SeatRowSpec(row_id='r1', name='A', count=3, spacing_m=0.8),
            SeatRowSpec(
                row_id='r2', name='B', count=3, spacing_m=0.8, row_spacing_m=1.0
            ),
        ),
    )
    params.update(overrides)
    return SeatingLayoutSpec(**params)


def test_seating_plan_is_deterministic_and_spaced(tmp_path) -> None:
    controller = _controller(tmp_path)
    spec = _seat_spec()
    plan = plan_seating(spec, controller.document)
    assert len(plan.slots) == 6
    xs = sorted(slot.position.x_m for slot in plan.slots[:3])
    assert xs[1] - xs[0] == pytest.approx(0.8)
    # Row 2 sits +1.0 m toward the rear (+Y).
    assert plan.slots[3].position.y_m - plan.slots[0].position.y_m == pytest.approx(1.0)
    # Second run is byte-identical.
    assert plan_seating(spec, controller.document).slots == plan.slots


def test_seating_aisle_adds_gap(tmp_path) -> None:
    controller = _controller(tmp_path)
    spec = _seat_spec(aisles=(AisleSpec(after_index=0, width_m=1.0),))
    plan = plan_seating(spec, controller.document)
    xs = [slot.position.x_m for slot in plan.slots[:3]]
    assert xs[1] - xs[0] == pytest.approx(0.8 + 1.0)
    assert xs[2] - xs[1] == pytest.approx(0.8)


def test_seating_riser_binding_requires_existing_riser(tmp_path) -> None:
    controller = _controller(tmp_path)
    spec = _seat_spec(
        rows=(
            SeatRowSpec(
                row_id='r1',
                name='A',
                count=2,
                spacing_m=0.8,
                riser_entity_id='riser-missing',
            ),
        )
    )
    with pytest.raises(SeatingLayoutError, match='ライザー'):
        plan_seating(spec, controller.document)


def test_seating_riser_binding_sets_top_height(tmp_path) -> None:
    controller = _controller(tmp_path)
    riser = SceneEntity(
        entity_id='riser-1',
        kind='riser',
        name='Riser',
        position=Position3(x_m=3.0, y_m=2.5, z_m=0.1),
        size_m=Size3(x_m=4.0, y_m=1.2, z_m=0.2),
    )
    controller.working.apply_entity_set_edit(added=(riser,))
    spec = _seat_spec(
        rows=(
            SeatRowSpec(
                row_id='r1',
                name='A',
                count=2,
                spacing_m=0.8,
                riser_entity_id='riser-1',
            ),
        )
    )
    plan = plan_seating(spec, controller.document)
    # Riser top = z 0.1 + 0.2/2 = 0.2; seat z = top + seat_height/2 = 0.2+0.45.
    assert plan.slots[0].position.z_m == pytest.approx(0.65)


def test_seating_regen_diff_and_manual_seats_untouched(tmp_path) -> None:
    controller = _controller(tmp_path)
    spec = _seat_spec()
    diff = apply_seating_layout(
        controller.working, controller.document, spec
    )
    assert len(diff.added) == 6
    # Manual seat not owned by the spec survives untouched.
    manual = SceneEntity(
        entity_id='seat-manual',
        kind='seat',
        name='手動座席',
        position=Position3(x_m=0.4, y_m=3.5, z_m=0.45),
        size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
    )
    controller.working.apply_entity_set_edit(added=(manual,))

    bigger = _seat_spec(
        rows=(SeatRowSpec(row_id='r1', name='A', count=5, spacing_m=0.8),)
    )
    diff2 = plan_regeneration(bigger, controller.document)
    # Slots keep stable ids: the three r1 seats are 'moved' (spec_id identical).
    assert all(
        spec_owns_entity(_seat_spec(), entity.entity_id)
        for entity in diff2.moved_after
    )
    assert len(diff2.added) == 2
    # Row-2 seats vanish from the plan -> removed candidates only.
    assert len(diff2.removed) == 3
    # Applied without remove_orphaned: they stay.
    apply_seating_layout(
        controller.working, controller.document, bigger
    )
    assert 'seat-manual' in {e.entity_id for e in controller.document.entities}
    assert 'seat-seatlayout-test01-r2-0' in {
        e.entity_id for e in controller.document.entities
    }
    # Generated seats keep listener semantics.
    seat = controller.document.entity('seat-seatlayout-test01-r1-0')
    assert seat.acoustic_reference_offset_m is not None
    assert seat.acoustic_reference_offset_m.z_m == pytest.approx(0.65)


# --- #618: authoring constraints ------------------------------------------------


def test_centerline_constraint_snaps_subject_back(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_centerline_constraint('x', ('furniture-left',))
    )
    # Move the furniture off the centerline; the constraint pulls it back.
    entity = controller.document.entity('furniture-left')
    controller.working.apply_entity_set_edit(
        replaced_before=(entity,),
        replaced_after=(
            entity.model_copy(
                update={
                    'position': Position3(
                        x_m=0.2, y_m=entity.position.y_m, z_m=entity.position.z_m
                    )
                }
            ),
        ),
    )
    notes = controller.propagate_constraints({'furniture-left'})
    snapped = controller.document.entity('furniture-left')
    assert snapped.position.x_m == pytest.approx(CENTERLINE_X)
    assert notes


def test_symmetric_pair_mirrors_driver_to_subject(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'furniture-left')
    )
    fl = controller.document.entity('speaker-fl')
    moved = fl.model_copy(
        update={
            'position': Position3(x_m=1.0, y_m=0.9, z_m=1.05)
        }
    )
    controller.working.apply_entity_set_edit(
        replaced_before=(fl,), replaced_after=(moved,)
    )
    controller.propagate_constraints({'speaker-fl'})
    subject = controller.document.entity('furniture-left')
    assert subject.position.x_m == pytest.approx(2 * CENTERLINE_X - 1.0)
    assert subject.position.y_m == pytest.approx(0.9)


def test_fixed_distance_constraint_keeps_axis_delta(tmp_path) -> None:
    controller = _controller(tmp_path)
    a = controller.document.entity('speaker-fl')
    b = controller.document.entity('speaker-c')
    constraint = make_fixed_distance_constraint(a, b, axis='x')
    controller.add_authoring_constraint(constraint)
    moved = a.model_copy(
        update={'position': Position3(x_m=1.6, y_m=a.position.y_m, z_m=a.position.z_m)}
    )
    controller.working.apply_entity_set_edit(
        replaced_before=(a,), replaced_after=(moved,)
    )
    controller.propagate_constraints({'speaker-fl'})
    after_b = controller.document.entity('speaker-c')
    # Delta x was 3.0-1.35=1.65; driver moved to 1.6 -> subject at 3.25.
    assert after_b.position.x_m == pytest.approx(1.6 + 1.65)
    assert after_b.position.y_m == pytest.approx(b.position.y_m)


def test_equal_spacing_constraint_respaces_interior(tmp_path) -> None:
    controller = _controller(tmp_path)
    constraint = make_equal_spacing_constraint(
        ('speaker-fl', 'speaker-c', 'speaker-fr'), axis='x'
    )
    controller.add_authoring_constraint(constraint)
    # Push the last endpoint outward; interior re-spaces between endpoints.
    fr = controller.document.entity('speaker-fr')
    moved = fr.model_copy(
        update={'position': Position3(x_m=5.0, y_m=fr.position.y_m, z_m=fr.position.z_m)}
    )
    controller.working.apply_entity_set_edit(
        replaced_before=(fr,), replaced_after=(moved,)
    )
    controller.propagate_constraints({'speaker-fr'})
    c = controller.document.entity('speaker-c')
    assert c.position.x_m == pytest.approx((1.35 + 5.0) / 2)


def test_constraint_breaks_on_member_delete_and_survives_persist(tmp_path) -> None:
    controller = _controller(tmp_path)
    constraint = make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    controller.add_authoring_constraint(constraint)
    fr = controller.document.entity('speaker-fr')
    controller.working.apply_entity_set_edit(removed=(fr,))
    broken = controller.mark_broken_constraints()
    assert broken
    stored = controller.repository.authoring_constraints(F1_DOCUMENT_ID)
    assert stored is not None
    parsed = AuthoringConstraintSet.model_validate(stored.payload)
    assert parsed.constraints[0].broken
    # Solve of a broken constraint is inert.
    result = solve_constraint(parsed.constraints[0], controller.document)
    assert result.moved == {} and result.rotated == {}


def test_guides_render_items_are_display_only(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_centerline_constraint('x', ('speaker-fl',))
    )
    items = controller.guide_render_items()
    assert len(items) == 1
    assert items[0].start[0] == pytest.approx(CENTERLINE_X)


# --- #629: isolation ------------------------------------------------------------


def test_isolation_round_trip_via_controller(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.isolate_entities({'speaker-fl'})
    assert controller.view_state.hidden_ids == {
        'speaker-c', 'speaker-fr', 'point-mlp', 'furniture-left'
    }
    controller.set_hidden_ids(set())
    assert controller.view_state.hidden_ids == set()
