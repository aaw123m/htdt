"""Numerical safety and consistency of bounded cut-cell Neumann FV prototype."""
from __future__ import annotations
import math

import numpy as np
import pytest
from scipy.sparse.linalg import eigsh

from htdt.r130d_embedded_neumann_fv import (
    SlopedPrism, build_sloped_embedded_neumann, interior_point_stencil,
    smooth_source_complex_transfer,
)


@pytest.mark.parametrize("n",[3,5,6,8,12])
def test_exact_sloped_room_volume_and_constant_neumann_mode(n):
    s=build_sloped_embedded_neumann(n)
    assert s.cell_volumes_m3.sum() == pytest.approx(56.0,abs=5e-12)
    assert np.all(s.cell_volumes_m3>0)
    assert np.all(s.fluid_face_areas_m2>0)
    assert s.degrees_of_freedom <= n**3
    if n >= 6:
        assert s.degrees_of_freedom < n**3
    c=np.ones(s.degrees_of_freedom)
    residual=s.stiffness@c
    diagonal=np.max(abs(s.stiffness.diagonal()))
    assert np.max(abs(residual))/diagonal < 1e-12
    assert np.max(np.abs((s.stiffness-s.stiffness.T).data),initial=0.0)<1e-9
    assert abs(float(c @ (s.stiffness @ c))) < 1e-7


def test_cut_cell_analytic_roof_plane_is_not_staircase():
    s=build_sloped_embedded_neumann(8)
    h=s.grid_spacing_m
    assert s.cell_volumes_m3.max()==pytest.approx(h**3)
    assert s.cell_volumes_m3.min()<h**3
    assert s.cell_volumes_m3.min()>0
    assert np.any((s.fluid_face_areas_m2>1e-10)
                  & (s.fluid_face_areas_m2<h*h-1e-10))


def _first_positive_frequencies(s,count=3):
    lam=eigsh(s.stiffness,M=s.mass,k=count+2,sigma=-1,which="LM",
              return_eigenvectors=False)
    return np.sort(np.sqrt(np.maximum(0,lam))/(2*np.pi))[1:count+1]


def test_flat_neumann_room_modes_against_exact_discrete_symbol():
    s=build_sloped_embedded_neumann(8,geometry=SlopedPrism(
        roof_drop_per_y_m=0.0))
    assert s.degrees_of_freedom == 8**3
    assert float(s.cell_volumes_m3.sum())==pytest.approx(64.0)
    got=_first_positive_frequencies(s,3)
    expected=343.2/(np.pi*s.grid_spacing_m)*np.sin(np.pi/(2*8))
    np.testing.assert_allclose(got,expected,atol=1e-6,rtol=1e-9)


def test_sloped_low_mode_approaches_independent_mfem_frequency():
    target=41.9606 # pinned independent MFEM level3 from #938 (NOT exact limit)
    frequencies=[float(_first_positive_frequencies(
        build_sloped_embedded_neumann(n),1)[0]) for n in (6,8,12,16)]
    assert all(abs(frequencies[i+1]-target)<abs(frequencies[i]-target)
               for i in range(3))
    assert abs(frequencies[-1]-target)<0.08


def test_implicit_midpoint_conserves_discrete_energy_even_at_large_dt():
    s=build_sloped_embedded_neumann(8)
    state=np.cos(np.pi*s.cell_center_xyz_m[:,0]/4)
    vel=np.zeros_like(state)
    solver=s.midpoint_integrator(0.005)
    e0=s.energy(state,vel)
    assert e0>0
    for _ in range(100):
        state,vel=solver.step(state,vel)
        assert np.all(np.isfinite(state))
    assert abs(s.energy(state,vel)/e0-1)<1e-9


def test_constant_potential_is_stationary_for_neumann_system():
    s=build_sloped_embedded_neumann(6)
    p=np.ones(s.degrees_of_freedom)
    v=np.zeros_like(p)
    solver=s.midpoint_integrator(0.01)
    p2,v2=solver.step(p,v)
    np.testing.assert_allclose(p2,p,atol=1e-12)
    np.testing.assert_allclose(v2,0,atol=1e-9)


def test_point_interpolation_partition_and_outside_rejected():
    s=build_sloped_embedded_neumann(8)
    w=interior_point_stencil(s,(1.5,2.0,2.0))
    assert w.sum()==pytest.approx(1)
    assert np.count_nonzero(w)<=8
    assert np.all(w>=0)
    with pytest.raises(ValueError):
        interior_point_stencil(s,(1.5,2.0,3.8))
    with pytest.raises(ValueError):
        interior_point_stencil(s,(0.0,2.0,2.0))


def test_actual_smooth_drive_not_postprocessed_and_guarded():
    s=build_sloped_embedded_neumann(6)
    transfer=smooth_source_complex_transfer(s,duration_s=0.03,
                                            time_step_s=0.00025)
    assert transfer.shape==(2,)
    assert np.all(np.isfinite(transfer))
    assert np.linalg.norm(transfer)>1e-3
    with pytest.raises(ValueError):
        smooth_source_complex_transfer(s,duration_s=0.0301,
                                       time_step_s=0.00025)
    with pytest.raises(ValueError):
        smooth_source_complex_transfer(s,frequency_hz=(2100.0,))


@pytest.mark.parametrize("n",[-1,2,1000,2.0,True])
def test_unsupported_resources_rejected(n):
    with pytest.raises(ValueError):
        build_sloped_embedded_neumann(n)


def test_invalid_slope_or_roof_is_rejected():
    for g in ({"roof_drop_per_y_m":-0.1},
              {"roof_drop_per_y_m":1.0},
              {"roof_height_at_y0_m":5.0},
              {"sound_speed_m_s":float("nan")}):
        with pytest.raises(ValueError):
            SlopedPrism(**g)
