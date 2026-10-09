"""Integrity and conservation of preregistered diagnostic spatial Gaussian operators."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_fixed_spatial_kernel import (
    _cell_integrals, validate_plan, fv_functionals, impulse_transfer,
    build_sloped_embedded_neumann, EXPECTED_SIGMAS, EXPECTED_FV,EXPECTED_MFEM
)
PLAN=ROOT/"benchmarks/acoustics/r130d_fixed_spatial_kernel_plan_2026-10-09.json"

def load_plan():
    return validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))

def test_source_shape_and_original_authority_frozen():
    p=load_plan()
    assert p["temporal_source"]["waveform"]=="q[0]=1, q[n>0]=0"
    assert p["temporal_source"]["frequency_bins_hz"]==[40,80]
    assert p["authority"]["production_ready"] is False
    assert p["authority"]["original_fullband_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert tuple(p["spatial_levels"]["fv_n"])==EXPECTED_FV
    assert tuple(p["spatial_levels"]["independent_mfem_P2_r"])==EXPECTED_MFEM

@pytest.mark.parametrize("mutation",[
    lambda p:p["temporal_source"].update(waveform="q[0]=q[1]=1"),
    lambda p:p["temporal_source"].update(frequency_bins_hz=[40]),
    lambda p:p["spatial_kernel"].update(sigma_m=[0.5,0.7]),
    lambda p:p["authority"].update(production_ready=True),
    lambda p:p["spatial_levels"].update(fv_n=[12,32]),
])
def test_fail_closed_to_plan_changes(mutation):
    p=load_plan();mutation(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_cutcell_integrated_volume_normalizations_and_width_change():
    p=load_plan()
    system=build_sloped_embedded_neumann(12)
    funcs,diagnostics=fv_functionals(system,p)
    assert abs(diagnostics["total_quadrature_volume_m3"]-56)<1e-9
    assert diagnostics["max_cut_cell_volume_quadrature_error_m3"]<1e-10
    assert len(funcs)==2
    for b,r in funcs:
        assert b.shape==(system.degrees_of_freedom,)
        assert np.all(b>=0) and np.all(r>=0)
        np.testing.assert_allclose(sum(b),1,atol=1e-12)
        np.testing.assert_allclose(sum(r),1,atol=1e-12)
    assert np.linalg.norm(funcs[0][0]-funcs[1][0])>0.01
    assert np.linalg.norm(funcs[0][1]-funcs[1][1])>0.01

def test_narrow_roof_cut_volume_exact():
    from numpy.polynomial.legendre import leggauss
    p=load_plan();sys=build_sloped_embedded_neumann(12)
    for index,(i,j,k) in enumerate(sys.cell_coordinates_ijk):
        if j not in (0,11) and (index%37)!=0:continue
        v,integrals=_cell_integrals(int(i),int(j),int(k),12,leggauss(4),
          [p["geometry"]["source_xyz_m"],p["geometry"]["receiver_xyz_m"]],
          list(EXPECTED_SIGMAS))
        assert abs(v-sys.cell_volumes_m3[index])<1e-10
        assert np.all(np.isfinite(integrals))
        assert np.all(integrals>=0)

def test_full_temporal_midpoint_source_and_receiver_are_finite():
    p=load_plan()
    system=build_sloped_embedded_neumann(3)
    (b,r),_=fv_functionals(system,p)
    for pair in (b,r):
        short=copy.deepcopy(p);short["temporal_source"]["steps"]=12
        wave=impulse_transfer(system.mass,system.stiffness,pair[0],pair[1],
                              short,use_pcg=False)
        assert wave["sampled_time_impulse_q0_only"] is True
        assert wave["worst_true_relative_linear_residual"]<1e-8
        assert len(wave["complex_40_80_hz"])==2
        assert np.all(np.isfinite(wave["complex_40_80_hz"]))

def test_bad_receiver_fail_closed():
    p=load_plan()
    system=build_sloped_embedded_neumann(3)
    functionals,_=fv_functionals(system,p)
    b,r=functionals[0]
    short=copy.deepcopy(p);short["temporal_source"]["steps"]=4
    with pytest.raises(ValueError):
        impulse_transfer(system.mass,system.stiffness,b,r*0,short,use_pcg=False)
