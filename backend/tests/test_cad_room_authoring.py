"""Issue #456 regression tests: rich room geometry authoring.

Semantic room primitives (sloped ceilings, soffits, risers, partial-height
walls, adjacent regions) compile into the shared R120 polyhedral authority.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_room_authoring import (
    AdjacentRegionSpec,
    PartialHeightWallSpec,
    RiserSpec,
    RoomAuthoringModel,
    SlopedCeilingSpec,
    SoffitSpec,
    compile_room_authoring_to_r120,
)
from htdt.cad_scene import RoomPrism


_ROOM = RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0)


def _surface_keys(geometry) -> set[str]:
    return {surface.surface_key for surface in geometry.surfaces}


def test_rectangular_room_compiles_to_polyhedral() -> None:
    geometry = compile_room_authoring_to_r120(RoomAuthoringModel(room=_ROOM))
    assert geometry.source_geometry_kind == 'explicit_polyhedral'
    keys = _surface_keys(geometry)
    assert keys == {'floor', 'ceiling', 'wall:0', 'wall:1', 'wall:2', 'wall:3'}
    assert len(geometry.air_volumes) == 1
    assert geometry.air_volumes[0].region_id == 'main'
    assert set(geometry.air_volumes[0].boundary_surface_keys) == keys
    # Canonical vertex ordering preserved (4 floor + 4 ceiling = 8).
    assert len(geometry.vertices) == 8


def test_sloped_ceiling_produces_trapezoid_walls() -> None:
    model = RoomAuthoringModel(
        room=_ROOM,
        ceiling=SlopedCeilingSpec(
            slope_direction='y+', low_height_m=2.4, high_height_m=3.4,
        ),
    )
    geometry = compile_room_authoring_to_r120(model)
    ceiling = next(s for s in geometry.surfaces if s.surface_key == 'ceiling')
    zs = [geometry.vertices[i].point()[2] for i in ceiling.outer_vertex_indices]
    assert min(zs) == pytest.approx(2.4)
    assert max(zs) == pytest.approx(3.4)
    # Deterministic: same model → identical content-derived geometry id.
    again = compile_room_authoring_to_r120(model)
    assert again.geometry_id == geometry.geometry_id


def test_riser_carves_floor_hole_and_sides() -> None:
    model = RoomAuthoringModel(
        room=_ROOM,
        risers=(RiserSpec(
            riser_id='riser-1', min_x_m=2.0, min_y_m=3.0,
            max_x_m=6.0, max_y_m=5.0, height_m=0.2,
        ),),
    )
    geometry = compile_room_authoring_to_r120(model)
    keys = _surface_keys(geometry)
    assert 'riser:riser-1:top' in keys
    assert sum(k.startswith('riser:riser-1:side:') for k in keys) == 4
    floor = next(s for s in geometry.surfaces if s.surface_key == 'floor')
    assert len(floor.hole_vertex_indices) == 1  # the riser footprint


def test_soffit_emits_bottom_and_sides() -> None:
    model = RoomAuthoringModel(
        room=_ROOM,
        soffits=(SoffitSpec(
            soffit_id='beam', min_x_m=1.0, min_y_m=1.0,
            max_x_m=3.0, max_y_m=2.0, drop_m=0.35,
        ),),
    )
    geometry = compile_room_authoring_to_r120(model)
    keys = _surface_keys(geometry)
    assert 'soffit:beam:bottom' in keys
    bottom = next(s for s in geometry.surfaces if s.surface_key == 'soffit:beam:bottom')
    zs = {geometry.vertices[i].point()[2] for i in bottom.outer_vertex_indices}
    assert len(zs) == 1
    assert zs.pop() == pytest.approx(2.65)


def test_partial_height_wall_emits_prism_faces() -> None:
    model = RoomAuthoringModel(
        room=_ROOM,
        partial_walls=(PartialHeightWallSpec(
            wall_id='pw', x1_m=2.0, y1_m=2.0, x2_m=4.0, y2_m=2.0,
            thickness_m=0.1, height_m=1.2,
        ),),
    )
    geometry = compile_room_authoring_to_r120(model)
    keys = _surface_keys(geometry)
    assert 'partial-wall:pw:top' in keys
    assert sum(k.startswith('partial-wall:pw:face:') for k in keys) == 4


def test_adjacent_region_creates_portal_and_volumes() -> None:
    model = RoomAuthoringModel(
        room=_ROOM,
        adjacent_regions=(AdjacentRegionSpec(
            region_id='lobby', shared_edge_index=1,
            outward_depth_m=2.0, opening=(1.0, 1.5, 2.2),
        ),),
    )
    geometry = compile_room_authoring_to_r120(model)
    keys = _surface_keys(geometry)
    # Shared wall splits into flanking surfaces + lintel + opening portal.
    assert 'wall:1:flank-a' in keys
    assert 'wall:1:flank-b' in keys
    assert 'wall:1:lintel' in keys
    assert 'wall:1:opening' in keys
    assert 'wall:1' not in keys  # replaced by the split surfaces
    assert {v.region_id for v in geometry.air_volumes} == {'main', 'lobby'}
    assert len(geometry.portals) == 1
    portal = geometry.portals[0]
    assert set(portal.region_ids) == {'main', 'lobby'}
    lobby = next(v for v in geometry.air_volumes if v.region_id == 'lobby')
    assert 'region:lobby:floor' in lobby.boundary_surface_keys
    assert 'wall:1:opening' in lobby.boundary_surface_keys


def test_model_validation_guards() -> None:
    with pytest.raises(ValidationError, match='flat ceiling'):
        RoomAuthoringModel(
            room=_ROOM,
            ceiling=SlopedCeilingSpec(
                slope_direction='x+', low_height_m=3.0, high_height_m=3.0,
            ),
        )
    with pytest.raises(ValidationError, match='below the room ceiling'):
        RoomAuthoringModel(
            room=_ROOM,
            risers=(RiserSpec(riser_id='r', min_x_m=0, min_y_m=0,
                              max_x_m=1, max_y_m=1, height_m=3.5),),
        )
    with pytest.raises(ValidationError, match='reserved'):
        RoomAuthoringModel(
            room=_ROOM,
            adjacent_regions=(AdjacentRegionSpec(
                region_id='main', shared_edge_index=0,
                outward_depth_m=2.0, opening=(0.0, 1.0, 2.0),
            ),),
        )
    with pytest.raises(ValidationError, match='unknown footprint edge'):
        RoomAuthoringModel(
            room=_ROOM,
            adjacent_regions=(AdjacentRegionSpec(
                region_id='x', shared_edge_index=9,
                outward_depth_m=2.0, opening=(0.0, 1.0, 2.0),
            ),),
        )
