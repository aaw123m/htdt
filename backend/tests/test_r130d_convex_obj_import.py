"""Strict convex watertight CAD OBJ import with reproducible source hashes."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from htdt.r130d_convex_obj_import import import_convex_obj_air_mesh
from htdt.r130d_embedded_neumann_convex import (
    build_convex_embedded_neumann,ConvexPlanarRoom,
)
from htdt.r130d_embedded_neumann_fv import smooth_source_complex_transfer

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=ROOT/"benchmarks"/"acoustics"/"r130d_convex_obj_fixtures"


@pytest.mark.parametrize("name,expected_volume,planes,sha",[
    ("planar_wedge",149/3,2,
     "396f7f6e6119a7e7062f01c3af32827e0ecefb985f434c488363e6609cb5babb"),
    ("three_axis_diagonal",160/3,1,
     "3cd0e5eead16208461e88a03ee6c41d39545e1b0fcb71b5d7027bb95452d58cb"),
])
def test_actual_obj_mesh_roundtrip_to_precise_neumann_operator(
    name,expected_volume,planes,sha,
):
    imported=import_convex_obj_air_mesh(FIXTURE/(name+".obj"))
    assert imported.source_sha256==sha
    assert imported.imported_format=="bounded-watertight-convex-triangle-obj-1"
    assert imported.plane_count==planes
    assert imported.surface_signed_volume_m3==pytest.approx(expected_volume,abs=1e-10)
    assert imported.convex_hull_volume_m3==pytest.approx(expected_volume,abs=1e-10)
    built=build_convex_embedded_neumann(8,geometry=imported.geometry)
    direct=build_convex_embedded_neumann(
        8,geometry=ConvexPlanarRoom(
            ((0,.25,1,4),(1,1,0,6)) if name=="planar_wedge"
            else ((1,1,1,8),)
        ),
    )
    np.testing.assert_array_equal(built.cell_coordinates_ijk,
                                  direct.cell_coordinates_ijk)
    np.testing.assert_allclose(built.cell_volumes_m3,
                               direct.cell_volumes_m3,atol=1e-12,rtol=1e-12)
    delta=built.stiffness-direct.stiffness
    assert np.linalg.norm(delta.data)/np.linalg.norm(direct.stiffness.data)<1e-11
    z=smooth_source_complex_transfer(
        built,frequency_hz=(40,80),duration_s=.25,
        time_step_s=.00025,
    )
    assert np.all(np.isfinite(z))


def _tamper(tmp_path,original:str):
    file=tmp_path/"invalid.obj"
    file.write_text(original,encoding="ascii")
    return file


def test_reject_open_manifold_face_missing(tmp_path):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    lines=source.splitlines()
    i=next(i for i,v in enumerate(lines) if v.startswith("f "))
    lines.pop(i)
    with pytest.raises(ValueError,match="manifold"):
        import_convex_obj_air_mesh(_tamper(tmp_path,"\n".join(lines)+"\n"))


def test_reject_inward_winding_and_signed_volume(tmp_path):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    lines=source.splitlines()
    i=next(i for i,v in enumerate(lines) if v.startswith("f "))
    face=lines[i].split()
    lines[i]=" ".join([face[0],face[1],face[3],face[2]])
    with pytest.raises(ValueError,match="manifold|outward"):
        import_convex_obj_air_mesh(_tamper(tmp_path,"\n".join(lines)+"\n"))


def test_reject_duplicate_faces_and_nonmanifold_edges(tmp_path):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    face=next(line for line in source.splitlines() if line.startswith("f "))
    with pytest.raises(ValueError,match="manifold"):
        import_convex_obj_air_mesh(_tamper(tmp_path,source+face+"\n"))


@pytest.mark.parametrize("illegal_line",[
    "f 1/1/1 2/1/1 3/1/1","f -1 -2 -3",
    "v NaN 0 0","v 5 0 0","vt 0.5 0.5",
    "f 1 2 3 4","usemtl absorbed-wall",
])
def test_reject_unsupported_mesh_features(tmp_path,illegal_line):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    with pytest.raises(ValueError):
        import_convex_obj_air_mesh(_tamper(tmp_path,source+illegal_line+"\n"))


def test_reject_non_obj_and_wrong_bounds(tmp_path):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    notobj=tmp_path/"mesh.stl"
    notobj.write_text(source)
    with pytest.raises(ValueError):
        import_convex_obj_air_mesh(notobj)
    with pytest.raises(ValueError):
        import_convex_obj_air_mesh(_tamper(tmp_path,source),length_m=3.5)


def test_reject_concavity_from_triangle_normal_violation(tmp_path):
    source=(FIXTURE/"planar_wedge.obj").read_text()
    # This inward perturbation of a hull vertex retains face winding and
    # manifold edge counts, but several original triangle planes stop
    # being global supports of a convex room.
    lines=source.splitlines()
    assert "v 4 2 3.5" in lines
    i=lines.index("v 4 2 3.5")
    lines[i]="v 1.5 2.0 2.0"
    with pytest.raises(ValueError):
        import_convex_obj_air_mesh(_tamper(tmp_path,"\n".join(lines)+"\n"))
