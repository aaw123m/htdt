"""Q2 polynomial-exact discrete acoustic point experiment; not original qualification."""
from __future__ import annotations
import json
import math
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_point_quadratic_pffdtd import q2_stencil,validate_plan,PPW,PIN

P=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_plan_2026-10-09.json"

def frozen():
    return validate_plan(json.loads(P.read_text(encoding="utf-8")))

def test_q2_pffdtd_original_point_experiment_failclosed():
    p=frozen()
    assert p["frozen_ppw"]==list(PPW)
    assert p["identities"]["pinned_upstream"]==PIN
    assert p["source_fixture"]["source_xyz_m"]==[1.5,2,2]
    assert p["source_fixture"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["authority"]["production_ready"] is False
    assert p["authority"]["canonical_original_8_node_point_point"]=="SELF_CONVERGENCE_FAILED"
    assert p["comparisons"]["original_numeric_thresholds"]=={
        "complex_rms_relative_max":.2,
        "magnitude_max_relative":.25,
        "phase_max_deg":15}

@pytest.mark.parametrize("mutation",[
    lambda p:p.update(frozen_ppw=[28,32]),
    lambda p:p["numerical_delta"].update(axis_node_offsets=[-2,0,2]),
    lambda p:p["source_fixture"].update(source_xyz_m=[1.6,2,2]),
    lambda p:p["source_fixture"].update(frequency_hz=[40]),
    lambda p:p["comparisons"].update(original_numeric_thresholds={
        "complex_rms_relative_max":1,"magnitude_max_relative":1,"phase_max_deg":180}),
    lambda p:p["authority"].update(production_ready=True),
])
def test_q2_plan_mutation_fails_closed(mutation):
    p=frozen();mutation(p)
    with pytest.raises(ValueError):validate_plan(p)

@pytest.mark.parametrize("xyz",[
    (0.255,0.311,0.427),
    (0.250,0.300,0.400),
    (0.252,0.348,0.451),
    (0.201,0.399,0.498),
])
def test_tensor_q2_reproduces_all_quadratics_at_exact_physical_point(xyz):
    h=.05
    axes=[np.arange(30)*h]*3
    idx,w,meta=q2_stencil(axes,xyz)
    x=np.vstack([a for a in np.unravel_index(idx,(30,30,30))]).T*h
    assert idx.shape==(27,) and len(np.unique(idx))==27
    assert w.shape==(27,)
    assert meta["support_count"]==27
    assert meta["negative_node_count"]>=0
    assert sum(w)==pytest.approx(1,abs=1e-12)
    assert all(abs(x)<1e-12 for x in meta["moments"]["first_m"])
    assert all(abs(x)<1e-12 for x in meta["moments"]["axis_second_m2"])
    for axis in range(3):
        assert np.dot(w,x[:,axis])==pytest.approx(xyz[axis],abs=1e-12)
        assert np.dot(w,x[:,axis]**2)==pytest.approx(xyz[axis]**2,abs=1e-12)
    for i,j in ((0,1),(0,2),(1,2)):
        assert np.dot(w,x[:,i]*x[:,j])==pytest.approx(xyz[i]*xyz[j],abs=1e-12)

def test_q2_signed_weights_are_accepted_explicitly():
    axes=[np.arange(30)*.05]*3
    _,w,meta=q2_stencil(axes,(.267,.311,.435))
    assert meta["negative_node_count"]>0
    assert meta["absolute_weight_sum"]>1
    assert np.sum(w)==pytest.approx(1,abs=1e-12)

def test_q2_refuses_halo_overlap_and_boundary_overlap():
    axes=[np.arange(30)*.05]*3
    xyz=(.267,.311,.435)
    ids,_,_=q2_stencil(axes,xyz)
    with pytest.raises(ValueError,match="overlaps"):
        q2_stencil(axes,xyz,boundary_nodes=[ids[0]])
    with pytest.raises(ValueError,match="overlaps"):
        q2_stencil(axes,xyz,abc_nodes=[ids[1]])

def test_q2_refuses_source_near_mesh_edge_and_nonuniform_mesh():
    axes=[np.arange(30)*.05]*3
    with pytest.raises(ValueError,match="leave grid"):
        q2_stencil(axes,(.01,.3,.3))
    bad=[a.copy() for a in axes]
    bad[1][6]+=.001
    with pytest.raises(ValueError,match="not uniform"):
        q2_stencil(bad,(.3,.3,.3))
