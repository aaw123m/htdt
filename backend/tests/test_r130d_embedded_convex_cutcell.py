"""Fail-closed geometry/operator tests for convex planar CAD cut cells.

Original R130D full-impulse and existing candidate reference are not changed.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.sparse.linalg import eigsh

from htdt.r130d_embedded_neumann_convex import (
    ConvexPlanarRoom,build_convex_embedded_neumann,
)
from htdt.r130d_embedded_neumann_fv import (
    SlopedPrism, build_sloped_embedded_neumann,interior_point_stencil,
    smooth_source_complex_transfer,
)


CASES = (
    ("baseline",((0,0.25,1,4),),56.0),
    ("wedge",((0,0.25,1,4),(1,1,0,6)),149/3),
    ("diagonal",((1,1,1,8),),160/3),
)


@pytest.mark.parametrize("name,planes,volume",CASES)
@pytest.mark.parametrize("n",[6,8,12,16])
def test_exact_polyhedral_volume_and_conservative_neumann(name,planes,volume,n):
    s=build_convex_embedded_neumann(n,geometry=ConvexPlanarRoom(planes))
    assert s.cell_volumes_m3.sum()==pytest.approx(volume,rel=5e-12,abs=1e-12)
    assert np.min(s.cell_volumes_m3)>0
    assert np.max(s.cell_volumes_m3)<=s.grid_spacing_m**3*(1+1e-12)
    assert np.min(s.fluid_face_areas_m2)>0
    assert np.max(s.fluid_face_areas_m2)<=s.grid_spacing_m**2*(1+1e-12)
    assert s.degrees_of_freedom < n**3
    assert np.max(np.abs((s.stiffness-s.stiffness.T).data),initial=0.0)<1e-8
    null=s.stiffness@np.ones(s.degrees_of_freedom)
    assert max(abs(null))<1e-11*max(abs(s.stiffness.diagonal()))
    assert s.mass.diagonal().sum()==pytest.approx(volume,rel=5e-12)


@pytest.mark.parametrize("n",[6,8,12])
def test_unchanged_analytical_sloped_operator_is_reproduced(n):
    general=build_convex_embedded_neumann(
        n,geometry=ConvexPlanarRoom(((0,0.25,1,4),)))
    old=build_sloped_embedded_neumann(n)
    np.testing.assert_array_equal(general.cell_coordinates_ijk,
                                  old.cell_coordinates_ijk)
    np.testing.assert_allclose(general.cell_volumes_m3,old.cell_volumes_m3,
                               rtol=1e-12,atol=5e-14)
    diff=general.stiffness-old.stiffness
    norm=np.linalg.norm(old.stiffness.data)
    assert np.linalg.norm(diff.data)/norm<1e-12
    m=np.max(abs((general.stiffness-old.stiffness).data),initial=0)
    assert m<1e-9


def test_nonorthogonal_3d_cut_has_real_cut_volumes_and_faces():
    s=build_convex_embedded_neumann(
        8,geometry=ConvexPlanarRoom(((1,1,1,8),)))
    h=s.grid_spacing_m
    assert np.any((s.cell_volumes_m3 > 1e-9)
                  & (s.cell_volumes_m3<h**3-1e-9))
    assert np.any((s.fluid_face_areas_m2>1e-9)
                  & (s.fluid_face_areas_m2<h*h-1e-9))


def test_implicit_midpoint_energy_and_positive_semidefinite_wave():
    s=build_convex_embedded_neumann(
        8,geometry=ConvexPlanarRoom(((0,0.25,1,4),(1,1,0,6))))
    lam=eigsh(s.stiffness,M=s.mass,k=4,sigma=-1,which="LM",
              return_eigenvectors=False)
    assert min(lam)>-1e-7
    x=np.cos(np.pi*s.cell_center_xyz_m[:,0]/4)
    v=np.zeros_like(x)
    e0=s.energy(x,v)
    assert e0>0
    solver=s.midpoint_integrator(0.005)
    for _ in range(60):
        x,v=solver.step(x,v)
    assert abs(s.energy(x,v)/e0-1)<1e-9


def test_actual_gaussian_drive_and_true_geometry_authority():
    g=ConvexPlanarRoom(((0,0.25,1,4),(1,1,0,6)))
    s=build_convex_embedded_neumann(8,geometry=g)
    w=interior_point_stencil(s,(1.5,2,2))
    assert w.sum()==pytest.approx(1,abs=1e-12)
    assert np.all(w>=0)
    assert not g.contains(np.array([3.2,3.2,1.0]))
    with pytest.raises(ValueError,match="convex room"):
        interior_point_stencil(s,(3.2,3.2,1.0))
    vals=smooth_source_complex_transfer(s)
    assert len(vals)==2 and np.all(np.isfinite(vals))


def test_plane_rescaling_and_reordering_do_not_change_geometry():
    a=ConvexPlanarRoom(((0,0.25,1,4),(1,1,0,6)))
    b=ConvexPlanarRoom(((2,2,0,12),(0,0.75,3,12)))
    s1=build_convex_embedded_neumann(8,geometry=a)
    s2=build_convex_embedded_neumann(8,geometry=b)
    np.testing.assert_array_equal(s1.cell_coordinates_ijk,s2.cell_coordinates_ijk)
    np.testing.assert_allclose(s1.cell_volumes_m3,s2.cell_volumes_m3,
                               atol=1e-13,rtol=1e-13)
    assert np.linalg.norm((s1.stiffness-s2.stiffness).data)<1e-8


@pytest.mark.parametrize("planes",[
    (),
    ((0,0,0,3),),
    ((1,1,1,float("nan")),),
    ((1,0,0,0),), # zero-volume domain
    tuple((1,0,0,3) for _ in range(9)),
])
def test_invalid_or_unsupported_geometry_fails_closed(planes):
    if planes==((1,0,0,0),):
        room=ConvexPlanarRoom(planes)
        with pytest.raises(ValueError):
            build_convex_embedded_neumann(8,geometry=room)
    else:
        with pytest.raises(ValueError):
            ConvexPlanarRoom(planes)


@pytest.mark.parametrize("bad_n",[0,2,500,8.5,True])
def test_excess_resources_and_invalid_resolution_rejected(bad_n):
    with pytest.raises(ValueError):
        build_convex_embedded_neumann(
            bad_n,geometry=ConvexPlanarRoom(((0,0.25,1,4),)))


def test_reject_singular_solid_or_zero_strength_plane():
    with pytest.raises(ValueError):
        ConvexPlanarRoom(((1e-12,0,0,1),))


def test_resource_sliver_tolerance_checks_no_silent_voxel_discard():
    room=ConvexPlanarRoom(((0,0.25,1,4),(1,1,0,6)))
    with pytest.raises(ValueError):
        build_convex_embedded_neumann(12,geometry=room,
                                     min_cut_volume_fraction=0)
    with pytest.raises(ValueError):
        build_convex_embedded_neumann(12,geometry=room,max_cells=100)
