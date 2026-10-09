"""Exact planar cutcell native-grid q0 experimental solver tests.

No PFFDTD production kernel modifications, no GitHub Actions.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_native_exact_roof_fv_q0 import verify_plan,PPW
from htdt.r130d_embedded_neumann_fv import SlopedPrism
from htdt.r130d_native_grid_exact_roof_fv import (
    build_native_exact_roof_fv,implicit_newmark_original_q0)

PLAN=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json"
def plan():
    return verify_plan(json.loads(PLAN.read_text(encoding="utf-8")))

def test_new_native_cutcell_solver_original_q0_contract_is_frozen():
    p=plan()
    assert p["native_original"]["ppw"]==list(PPW)
    assert p["native_original"]["source_xyz_m"]==[1.5,2,2]
    assert p["native_original"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["native_original"]["frequencies_hz"]==[40,80]
    assert p["algorithm"]["CG_tolerance_rtol"]==1e-10
    assert p["algorithm"]["energy_drift_max"]==1e-6
    assert p["limits"]["no_github_actions_runs"] is True
    assert p["authority"]["original_eight_node_q0"]=="SELF_CONVERGENCE_FAILED"
    assert p["authority"]["product"]=="NO_GO"

@pytest.mark.parametrize("change",[
    lambda p:p["native_original"].update(ppw=[28,32]),
    lambda p:p["native_original"].update(source_xyz_m=[1.55,2,2]),
    lambda p:p["native_original"].update(frequencies_hz=[40]),
    lambda p:p["algorithm"].update(max_CG_iterations=2),
    lambda p:p["comparisons"].update(complex_relative_threshold=2),
    lambda p:p["limits"].update(no_github_actions_runs=False),
    lambda p:p["authority"].update(product="GO")
])
def test_native_exact_cutcell_plan_rejects_posthoc_changes(change):
    p=plan();change(p)
    with pytest.raises(ValueError):verify_plan(p)

@pytest.mark.parametrize("slope,roof,expected",[(.25,4.,56.),(0.,4.,64.)])
def test_native_grid_exact_sloped_and_flat_roof_volume_symmetric_neumann(slope,roof,expected):
    original_nodes=np.arange(-.45,4.56,.3,dtype=float)
    geom=SlopedPrism(roof_height_at_y0_m=roof,roof_drop_per_y_m=slope)
    s=build_native_exact_roof_fv((original_nodes,)*3,geometry=geom)
    assert s.room_fluid_volume_m3==pytest.approx(expected,rel=0,abs=1e-10)
    assert s.number_of_cells>2000
    assert s.native_grid_spacing_m==pytest.approx(.3,abs=1e-12)
    assert np.min(s.fluid_volume_m3)>0
    assert np.max(s.fluid_volume_m3)<=.3**3+1e-12
    assert s.min_volume_fraction>0
    assert (s.stiffness_matrix-s.stiffness_matrix.T).nnz==0
    np.testing.assert_allclose(s.stiffness_matrix@np.ones(s.number_of_cells),
        0,atol=1e-8)
    assert np.min(s.exact_flux_face_open_area_m2)>0
    assert np.max(s.exact_flux_face_open_area_m2)<=.3**2+1e-12
    assert np.all(s.mass_matrix.diagonal()>0)
    u=np.linspace(-1,1,s.number_of_cells)
    assert float(u@(s.stiffness_matrix@u))>=-1e-8

def test_original_point_q0_implicit_newmark_stable_undamped_energy_exact():
    original_nodes=np.arange(-.45,4.56,.3,dtype=float)
    s=build_native_exact_roof_fv((original_nodes,)*3)
    native_lut=set(map(int,s.original_native_flat_indices))
    dims=s.native_dimensions
    # Original trilinear source and receiver, with all eight true neighbors.
    def stencil(x,y,z):
        centers=[int(np.searchsorted(original_nodes,w)) for w in (x,y,z)]
        support=np.array([np.ravel_multi_index((i,j,k),dims)
            for i in centers[0]-np.array([1,0])
            for j in centers[1]-np.array([1,0])
            for k in centers[2]-np.array([1,0])],dtype=int)
        assert all(int(x) in native_lut for x in support)
        w=np.ones(8)/8
        return support,w
    src,sw=stencil(1.5,2,2)
    recv,rw=stencil(2.5,2,2)
    trace,stats=implicit_newmark_original_q0(
        s,native_dt_s=.00025,native_source_ix=src,
        native_source_q0_weights=sw,native_receiver_ix=recv,
        native_receiver_weights=rw,native_record_samples=150,
        solver_rtol=1e-10,solver_atol=1e-12,max_cg_iter=500)
    assert trace.shape==(150,)
    assert np.all(np.isfinite(trace))
    assert trace[0]==0
    assert np.max(np.abs(trace[1:]))>0
    assert stats["maximum_CG_iterations"]<=500
    assert stats["maximum_true_linear_relative_residual"]<=5e-10
    assert stats["relative_energy_drift_after_source"]<=1e-6
    assert len(stats["newmark_midpoint_homogeneous_energy_probes"])==4
    assert stats["original_discrete_q0_unchanged"] is True
    assert stats["no_point_source_smoothing_or_taper"] is True

def test_cutcell_solver_rejects_illegal_grid_and_invalid_point_operator():
    x=np.arange(-.45,4.56,.3,dtype=float)
    with pytest.raises(ValueError):
        build_native_exact_roof_fv((x,x,x[::2]))
    with pytest.raises(ValueError):
        build_native_exact_roof_fv((x,x,x),max_nodes=100)
    s=build_native_exact_roof_fv((x,x,x))
    bad=np.zeros(8,dtype=int)
    with pytest.raises(ValueError):
        implicit_newmark_original_q0(
            s,native_dt_s=.00025,native_source_ix=bad,
            native_source_q0_weights=np.ones(8)/8,
            native_receiver_ix=bad,
            native_receiver_weights=np.ones(8)/8,
            native_record_samples=2)
