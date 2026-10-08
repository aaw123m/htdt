"""Issue #976 — semantic room authoring via the 「高度な形状」 panel section.

Coverage contract:
- all five primitive kinds (sloped ceiling / soffit / riser / knee wall /
  adjacent region) can be added through the panel + controller, persisted on
  SceneRevision, reloaded, numerically edited, and undone/redone;
- the committed model compiles to R120 semantic geometry whose surface keys,
  dimensions and region ids match the authoring intent;
- fail-closed validation (geometry_invalid / unsupported) leaves the
  committed document untouched and names the offending primitive;
- selection parity: the (kind, primitive_id) pair is shared between the
  panel, the 3D controller and the saved revision;
- backward compatibility: pre-existing flat-ceiling / single-room documents
  keep their hashes and load without the new field.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_room_authoring import (  # noqa: E402
    RoomAuthoringError,
    ceiling_height_at,
    compile_room_authoring_to_r120,
    validate_room_authoring_model,
)
from htdt.cad_scene import (  # noqa: E402
    F1_DOCUMENT_ID,
    AdjacentRegionSpec,
    PartialHeightWallSpec,
    RiserSpec,
    RoomAuthoringModel,
    SceneDocument,
    SlopedCeilingSpec,
    SoffitSpec,
    canonical_scene_json,
    make_f1_scene,
    room_vertices,
)
from htdt.room_geometry_input import (  # noqa: E402
    RoomGeometryInputController,
    _authoring_outline_segments,
    _authoring_primitive_ids,
)
from htdt.room_geometry_panel import RoomGeometryPanel  # noqa: E402
from htdt.room_viewport import _room_authoring_surface_meshes  # noqa: E402
from htdt.room_workspace import RoomWorkspace  # noqa: E402

from test_room_cadux import FakeRoomViewport, _f1_repository  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path) -> tuple:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return app, workspace, repository


def _panel(tmp_path):
    app, workspace, repository = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    return app, workspace, repository, geometry, panel


def _model_with(room, **updates) -> RoomAuthoringModel:
    return RoomAuthoringModel(room=room, **updates)


def test_document_field_is_optional_and_hash_stable() -> None:
    """Pre-#976 documents: no field, no schema bump, identical hash."""

    document = make_f1_scene()
    assert document.room_authoring is None
    payload_a = canonical_scene_json(document)
    assert 'room_authoring' not in payload_a

    # A document carrying authoring must declare schema >= 6.
    room = document.room
    model = _model_with(
        room, risers=(RiserSpec(riser_id='r1', min_x_m=1, min_y_m=1, max_x_m=2, max_y_m=2, height_m=0.2),)
    )
    with pytest.raises(ValidationError):
        SceneDocument.model_validate(
            {**document.model_dump(mode='python'), 'room_authoring': model.model_dump(mode='python')}
        )
    upgraded = SceneDocument.model_validate(
        {
            **document.model_dump(mode='python'),
            'schema_version': 6,
            'room_authoring': model.model_dump(mode='python'),
        }
    )
    assert upgraded.room_authoring == model


def test_all_five_kinds_add_apply_delete_undo_redo(tmp_path) -> None:
    _app, workspace, repository, geometry, panel = _panel(tmp_path)
    panel.authoring_toggle.setChecked(True)
    assert not panel.authoring_host.isHidden()

    # Distinct footprints — the validator rejects overlapping primitives.
    field_values = {
        'ceiling': {'low': 2.0, 'high': 2.4},
        'soffit': {'x1': 0.5, 'y1': 0.5, 'x2': 2.0, 'y2': 1.5, 'drop': 0.3},
        'riser': {'x1': 3.5, 'y1': 2.0, 'x2': 5.5, 'y2': 3.5, 'height': 0.2},
        'partial_wall': {
            'x1': 0.5, 'y1': 2.5, 'x2': 2.5, 'y2': 2.5,
            'base': 0.0, 'height': 1.0, 'thickness': 0.1,
        },
        'adjacent_region': {
            'depth': 2.0, 'height': 2.2,
            'open_offset': 0.5, 'open_width': 1.0, 'open_height': 1.8,
        },
    }
    adds = {
        'ceiling': lambda m: m.ceiling,
        'soffit': lambda m: len(m.soffits),
        'riser': lambda m: len(m.risers),
        'partial_wall': lambda m: len(m.partial_walls),
        'adjacent_region': lambda m: len(m.adjacent_regions),
    }
    ids: dict[str, str] = {}
    for kind, extract in adds.items():
        panel.authoring_kind.setCurrentIndex(panel.authoring_kind.findData(kind))
        panel._populate_authoring_selector()
        fields = getattr(
            panel,
            {
                'ceiling': '_auth_ceiling_fields',
                'soffit': '_auth_soffit_fields',
                'riser': '_auth_riser_fields',
                'partial_wall': '_auth_wall_fields',
                'adjacent_region': '_auth_region_fields',
            }[kind],
        )
        for key, value in field_values[kind].items():
            fields[key].set_value_m(value)
        panel._add_authoring()
        model = workspace.controller.committed_document.room_authoring
        assert model is not None, kind
        assert extract(model), kind
        item = _authoring_items(model, kind)[-1]
        ids[kind] = item
        panel.authoring_selector.setCurrentIndex(
            panel.authoring_selector.findData(item)
        )
        panel._apply_authoring()  # numeric re-commit round
        assert workspace.controller.committed_document.room_authoring is not None

    assert workspace.controller.committed_document.schema_version >= 6

    # Selection parity: panel selector id == 3D controller (kind, id).
    model = workspace.controller.committed_document.room_authoring
    wall = model.partial_walls[0]
    geometry.select_authoring_primitive(('partial_wall', wall.wall_id))
    assert geometry.selected_authoring == ('partial_wall', wall.wall_id)

    # Delete one kind, undo, redo.
    panel.authoring_kind.setCurrentIndex(
        panel.authoring_kind.findData('partial_wall')
    )
    panel._populate_authoring_selector()
    panel.authoring_selector.setCurrentIndex(
        panel.authoring_selector.findData(wall.wall_id)
    )
    panel._delete_authoring()
    assert not workspace.controller.committed_document.room_authoring.partial_walls
    workspace.controller.undo()
    assert workspace.controller.committed_document.room_authoring.partial_walls
    workspace.controller.redo()
    assert not workspace.controller.committed_document.room_authoring.partial_walls


def test_revision_round_trip_preserves_authoring(tmp_path) -> None:
    _app, workspace, repository, geometry, panel = _panel(tmp_path)
    room = geometry.room
    model = _model_with(
        room,
        ceiling=SlopedCeilingSpec(slope_direction='y+', low_height_m=2.0, high_height_m=2.6),
        risers=(
            RiserSpec(riser_id='stage', min_x_m=0.5, min_y_m=2.5, max_x_m=5.5, max_y_m=3.5, height_m=0.3),
        ),
        partial_walls=(
            PartialHeightWallSpec(
                wall_id='bar', x1_m=0.5, y1_m=1.5, x2_m=2.5, y2_m=1.5,
                thickness_m=0.1, height_m=1.1,
            ),
        ),
        adjacent_regions=(
            AdjacentRegionSpec(
                region_id='annex', shared_edge_index=1, outward_depth_m=2.0,
                opening=(0.5, 1.5, 1.9), ceiling_height_m=2.2,
            ),
        ),
    )
    assert workspace.controller.replace_room_authoring(model)

    head = repository.current_head(F1_DOCUMENT_ID)
    saved = repository.save(
        workspace.controller.committed_document,
        parent_revision_id=head.revision_id,
    )
    reloaded = repository.get(saved.revision.revision_id)
    assert reloaded is not None
    restored = reloaded.document
    assert restored.room_authoring == model
    assert restored.schema_version >= 6

    # The reloaded model compiles to the identical R120 geometry.
    assert (
        compile_room_authoring_to_r120(restored.room_authoring)
        == compile_room_authoring_to_r120(model)
    )


def test_compiled_geometry_matches_authoring_ids_and_dimensions() -> None:
    document = make_f1_scene()
    room = document.room
    model = _model_with(
        room,
        ceiling=SlopedCeilingSpec(slope_direction='x+', low_height_m=1.9, high_height_m=2.4),
        risers=(
            RiserSpec(riser_id='pit', min_x_m=1.0, min_y_m=1.0, max_x_m=2.0, max_y_m=2.0, height_m=0.25),
        ),
        adjacent_regions=(
            AdjacentRegionSpec(
                region_id='hall', shared_edge_index=1, outward_depth_m=2.5,
                opening=(0.6, 1.2, 1.8),
            ),
        ),
    )
    geometry = compile_room_authoring_to_r120(model)
    keys = {surface.surface_key for surface in geometry.surfaces}

    assert 'ceiling' in keys
    assert 'riser:pit:top' in keys
    assert {f'riser:pit:side:{i}' for i in range(4)} <= keys
    assert {
        'region:hall:floor',
        'region:hall:ceiling',
        'region:hall:outer',
        'region:hall:side-a',
        'region:hall:side-b',
    } <= keys
    # The portal surface is the wall's opening segment; the acoustic portal
    # object carries the region id.
    assert 'wall:1:opening' in keys
    assert {'wall:1:flank-a', 'wall:1:flank-b', 'wall:1:lintel'} <= keys

    # Sloped ceiling height varies along +X as specified.
    low = ceiling_height_at(model, *document.room.bounds_m[:1], 0)
    assert abs(low - 1.9) < 1e-9
    assert abs(ceiling_height_at(model, room.bounds_m[2], 0.0) - 2.4) < 1e-9

    # Region volume + portal carry the region id into the acoustic model.
    volumes = {v.region_id for v in geometry.air_volumes}
    assert {'main', 'hall'} <= volumes
    portals = {p.portal_id for p in geometry.portals}
    assert 'portal:hall' in portals


def test_fail_closed_validation_names_the_offender(tmp_path) -> None:
    _app, workspace, repository, geometry, panel = _panel(tmp_path)
    room = geometry.room
    committed = workspace.controller.committed_document

    # A riser overlapping a knee wall footprint is rejected; document untouched.
    model = _model_with(
        room,
        risers=(
            RiserSpec(riser_id='r1', min_x_m=1.0, min_y_m=1.0, max_x_m=3.0, max_y_m=2.0, height_m=0.2),
        ),
        partial_walls=(
            PartialHeightWallSpec(
                wall_id='w1', x1_m=2.0, y1_m=0.5, x2_m=2.0, y2_m=2.5,
                thickness_m=0.1, height_m=1.0,
            ),
        ),
    )
    issues = validate_room_authoring_model(model)
    errors = [issue for issue in issues if issue.severity == 'error']
    assert errors, 'expected a geometry_invalid error'
    assert any('w1' in issue.message for issue in errors)
    with pytest.raises(RoomAuthoringError):
        workspace.controller.replace_room_authoring(model)
    assert workspace.controller.committed_document == committed

    # Riser taller than the room fails at the model level.
    with pytest.raises(ValidationError):
        _model_with(
            room,
            risers=(
                RiserSpec(riser_id='r2', min_x_m=0.5, min_y_m=0.5, max_x_m=1.5, max_y_m=1.5, height_m=room.height_m + 1),
            ),
        )

    # An adjacent region whose portal top exceeds the local ceiling is refused.
    bad_region = _model_with(
        room,
        ceiling=SlopedCeilingSpec(slope_direction='x-', low_height_m=1.6, high_height_m=2.4),
        adjacent_regions=(
            AdjacentRegionSpec(
                region_id='wing', shared_edge_index=1, outward_depth_m=2.0,
                opening=(0.5, 1.0, 2.3),
            ),
        ),
    )
    issues = validate_room_authoring_model(bad_region)
    assert any(issue.code == 'opening_above_ceiling' for issue in issues)
    with pytest.raises(RoomAuthoringError):
        workspace.controller.replace_room_authoring(bad_region)
    assert workspace.controller.committed_document == committed


def test_room_edit_rebinds_or_rejects_authoring(tmp_path) -> None:
    _app, workspace, repository, geometry, panel = _panel(tmp_path)
    room = geometry.room
    vertices = room_vertices(room)
    region = AdjacentRegionSpec(
        region_id='annex', shared_edge_index=len(vertices) - 1,
        outward_depth_m=1.5, opening=(0.2, 0.8, 1.8),
    )
    model = _model_with(room, adjacent_regions=(region,))
    assert workspace.controller.replace_room_authoring(model)

    # A pure height change rebinds cleanly (same edges).
    taller = type(room).model_validate(
        room.model_copy(update={'height_m': room.height_m + 0.5}).model_dump(mode='python')
    )
    working = workspace.controller.working
    assert working.replace_room(taller)
    rebound = workspace.controller.committed_document.room_authoring
    assert rebound is not None and rebound.room == taller

    # Shrinking the polygon below the region's shared edge is refused —
    # nothing partial is committed.
    from htdt.cad_scene import make_polygon_room

    triangle = make_polygon_room(
        vertices[:3], height_m=taller.height_m, room_id=room.room_id
    )
    with pytest.raises(RoomAuthoringError):
        working.replace_room(triangle)
    assert workspace.controller.committed_document.room == taller


def test_panel_warning_surfaces_for_unbound_materials(tmp_path) -> None:
    _app, workspace, repository, geometry, panel = _panel(tmp_path)
    panel.authoring_toggle.setChecked(True)
    panel.authoring_kind.setCurrentIndex(panel.authoring_kind.findData('riser'))
    panel._populate_authoring_selector()
    panel._add_authoring()
    assert '材質' in panel.notice.text()  # material_binding_unsupported warning


def test_3d_overlay_segments_cover_committed_primitives() -> None:
    document = make_f1_scene()
    room = document.room
    model = _model_with(
        room,
        ceiling=SlopedCeilingSpec(slope_direction='x+', low_height_m=2.0, high_height_m=2.4),
        soffits=(SoffitSpec(soffit_id='s1', min_x_m=0.5, min_y_m=0.5, max_x_m=2.0, max_y_m=2.0, drop_m=0.3),),
        risers=(RiserSpec(riser_id='r1', min_x_m=3.0, min_y_m=0.5, max_x_m=5.0, max_y_m=2.0, height_m=0.2),),
        partial_walls=(
            PartialHeightWallSpec(
                wall_id='w1', x1_m=0.5, y1_m=1.0, x2_m=1.5, y2_m=1.0,
                thickness_m=0.1, height_m=1.0,
            ),
        ),
        adjacent_regions=(
            AdjacentRegionSpec(
                region_id='a1', shared_edge_index=1, outward_depth_m=2.0,
                opening=(0.5, 1.0, 1.8), ceiling_height_m=2.2,
            ),
        ),
    )
    refs = {ref for ref, _segments in _authoring_outline_segments(model)}
    assert refs == {
        ('ceiling', 'ceiling'),
        ('soffit', 's1'),
        ('riser', 'r1'),
        ('partial_wall', 'w1'),
        ('adjacent_region', 'a1'),
    }
    ids = _authoring_primitive_ids(model)
    assert ids['ceiling'] == ('ceiling',)
    assert ids['adjacent_region'] == ('a1',)


def test_viewport_surface_meshes_render_primitives_only() -> None:
    document = make_f1_scene()
    room = document.room
    model = _model_with(
        room,
        risers=(RiserSpec(riser_id='r1', min_x_m=3.0, min_y_m=0.5, max_x_m=5.0, max_y_m=2.0, height_m=0.2),),
        adjacent_regions=(
            AdjacentRegionSpec(
                region_id='a1', shared_edge_index=1, outward_depth_m=2.0,
                opening=(0.5, 1.0, 1.8),
            ),
        ),
    )
    upgraded = SceneDocument.model_validate(
        {
            **document.model_dump(mode='python'),
            'schema_version': 6,
            'room_authoring': model.model_dump(mode='python'),
        }
    )
    meshes = _room_authoring_surface_meshes(upgraded)
    keys = {key for key, _kind, _mesh in meshes}
    assert 'riser:r1:top' in keys
    assert 'region:a1:floor' in keys
    # The portal renders as the shared wall's opening segment.
    assert 'wall:1:opening' in keys
    assert 'floor' not in keys and 'wall:1' not in keys
    for _key, _kind, mesh in meshes:
        assert mesh.n_points >= 3 and mesh.n_cells >= 1


def _authoring_items(model: RoomAuthoringModel, kind: str) -> list[str]:
    if kind == 'ceiling':
        return ['ceiling'] if model.ceiling is not None else []
    key = {
        'soffit': ('soffits', 'soffit_id'),
        'riser': ('risers', 'riser_id'),
        'partial_wall': ('partial_walls', 'wall_id'),
        'adjacent_region': ('adjacent_regions', 'region_id'),
    }[kind]
    return [getattr(item, key[1]) for item in getattr(model, key[0])]
