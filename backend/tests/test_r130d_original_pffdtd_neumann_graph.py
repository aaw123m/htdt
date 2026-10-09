"""Original pinned PFFDTD Cartesian Neumann stencil symmetry and provenance."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import h5py
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_pffdtd_neumann_graph_audit import (
    PPW,PIN,validate_plan,reconstruct_graph)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_neumann_graph_evidence_2026-10-09.json"

def frozen():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return p,d

def test_original_native_boundary_plan_is_frozen_and_no_go():
    p,d=frozen()
    assert p["geometry_source"]["five_frozen_original_ppw"]==list(PPW)
    assert p["geometry_source"]["pin_upstream_sha"]==PIN
    assert p["geometry_source"]["source_position_m"]==[1.5,2,2]
    assert p["matrix"]["do_not_change_boundary_or_solver"] is True
    assert p["authority"]["product"]=="NO_GO"
    assert d["preregistered_plan"]==p
    assert d["plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert d["pinned_unmodified_upstream"]==PIN
    assert d["original_fullband_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["production_ready"] is False
    assert d["upstream_pffdtd_source_changed"] is False

@pytest.mark.parametrize("change",[
    lambda p:p["geometry_source"].update(five_frozen_original_ppw=[28,32]),
    lambda p:p["matrix"].update(offset_order=["-Ny*Nz"]),
    lambda p:p["geometry_source"].update(pin_upstream_sha="0"*40),
    lambda p:p["gate"].update(expected_symmetry_mismatch_count=10),
    lambda p:p["authority"].update(product="GO"),
])
def test_graph_audit_plan_fails_closed_on_changes(change):
    p,_=frozen();change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_exact_original_room_graph_values_are_all_saved_even_if_unfavorable():
    p,d=frozen()
    rows=d["actual_original_voxel_grid_audits"]
    assert [q["ppw"] for q in rows]==list(PPW)
    other=json.loads((ROOT/p["geometry_source"]["committed_prior_two_native_wave_evidence"]).read_text(encoding="utf-8"))
    for row,point in zip(rows,other["actual_native_wave_cases"]):
        assert row["original_exact_voxel_sha256"]==point["original_solver_geometry_sha256"]
        assert row["source_connected_room_nodes"]>0
        assert row["source_connected_boundary_nodes"]>0
        assert row["connected_non_ghost_directed_edges"]>0
        assert row["max_native_constant_field_row_sum_abs"]<=1e-12
        assert row["max_original_numba_vs_independent_boundary_row_action_abs"]<=1e-10
        assert row["native_operator_relative_bilinear_skew"]>=0
        assert len(row["unreciprocated_directed_edge_examples"])<=15
        assert isinstance(row["any_operator_self_adjointness_defect"],dict)
        assert row["original_rigid_boundary_condition_unchanged"] is True

def mock_vox(omitting_direction=None):
    from io import BytesIO
    data=BytesIO()
    vox=h5py.File(data,"w")
    dims=(9,9,9)
    coords=(4,4,4)
    idx=np.ravel_multi_index(coords,dims)
    adj=np.ones((1,6),dtype=np.int64)
    if omitting_direction is not None:
        adj[0,omitting_direction]=0
    for name,size in zip(("Nx","Ny","Nz"),dims):
        vox.create_dataset(name,data=np.int64(size))
    for name in ("xv","yv","zv"):
        vox.create_dataset(name,data=np.linspace(0,4,9))
    vox.create_dataset("bn_ixyz",data=np.asarray([idx],dtype=np.int64))
    vox.create_dataset("adj_bn",data=adj)
    return vox

def test_synthetic_reciprocal_native_graph_without_walls_is_symmetric():
    p,_=frozen()
    with mock_vox() as vox:
        values=reconstruct_graph(p,vox,p["geometry_source"]["source_position_m"])
    dims,boundary,bn,adj,start,visited,edges,ghost,missing,rowsum,_=values
    assert dims==(9,9,9)
    assert len(visited)==343
    assert not missing
    assert rowsum==0
    assert ghost>0

def test_one_sided_boundary_adjacency_is_detected_not_silently_symmetric():
    p,_=frozen()
    with mock_vox(omitting_direction=0) as vox:
        values=reconstruct_graph(p,vox,p["geometry_source"]["source_position_m"])
    missing=values[8]
    assert len(missing)>0
    assert all("direction" in e and "i" in e and "j" in e for e in missing)
