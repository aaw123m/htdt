"""Independent full-state sparse original PFFDTD q0 solver vs native 8-node raw waves."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import sparse

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_native_sparse_fullstate import (
    PPW,PIN,validate_plan,independent_native_csr_leapfrog,relative_l2,pairs,unpairs)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_native_sparse_fullstate_independent_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_native_sparse_fullstate_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["preregistered_plan"]==p
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    return p,d

def test_independent_native_fullstate_all_five_original_ppw_and_no_actions():
    p,d=frozen()
    assert p["original_inputs"]["ppw"]==list(PPW)
    assert d["pffdtd_original_upstream"]==PIN
    assert d["github_actions_launched_for_experiment"]==0
    assert p["scope"]["github_actions_runs"]==0
    assert d["all_5_original_native_PFFDTD_8node_full_raw_waveforms_reproduced"] is True
    assert d["source_receiver_geometry_time_resolution_unchanged"] is True
    assert d["canonical_original_selfconvergence_still_failed"] is True
    assert d["original_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["production"]=="NO_GO"

@pytest.mark.parametrize("mutation",[
    lambda p:p["original_inputs"].update(ppw=[28,32]),
    lambda p:p["original_inputs"].update(physical_source_receiver_xyz_m=[[1.55,2,2],[2.5,2,2]]),
    lambda p:p["original_inputs"].update(frequencies_hz=[30,50]),
    lambda p:p["independent_solver"].update(max_raw_trace_relative_l2=.1),
    lambda p:p["independent_solver"].update(max_discrete_energy_relative_drift=1),
    lambda p:p["scope"].update(github_actions_runs=1),
    lambda p:p["release"].update(production="GO"),
])
def test_native_full_state_pre_registered_plan_mutation_rejected(mutation):
    p,_=frozen()
    mutation(p)
    with pytest.raises(ValueError):
        validate_plan(p)

def test_independent_all_grid_full_record_pressure_source_both_signed_bins_and_energy():
    p,d=frozen()
    graph=json.loads((ROOT/p["original_inputs"]["graph_evidence"]).read_text())
    waves=json.loads((ROOT/p["original_inputs"]["full_original_wave_control_evidence"]).read_text())
    cases=d["native_full_state_comparisons"]
    assert [x["ppw"] for x in cases]==list(PPW)
    assert [x["original_native_record_sample_count"] for x in cases]==[
        1214,1388,1561,1734,1908]
    for x,gr,wv in zip(cases,graph["actual_original_voxel_grid_audits"],waves["actual_native_wave_cases"]):
        assert x["ppw"]==gr["ppw"]==wv["ppw"]
        assert x["original_exact_vox_sha256"]==gr["original_exact_voxel_sha256"]
        assert x["original_exact_source_comms_sha256"]==wv["original_native_comm_sha256"]
        assert x["original_graph_room_nodes"]==gr["source_connected_room_nodes"]
        assert x["native_full_stencil_nonzeros"]==x["original_graph_room_nodes"]+gr["connected_non_ghost_directed_edges"]
        assert x["independent_full_room_wave_wall_seconds"]>0
        assert x["independent_full_state_reproduction_within_frozen_bounds"] is True
        assert x["independent_pinned_numba_kernels_not_called"] is True
        assert x["native_source_receiver_and_wall_unchanged"] is True
        assert x["original_8node_q0_diagnostic_no_promotion"] is True
        assert 0<x["all_eight_original_node_raw_trace_relative_l2"]<=1e-8
        assert 0<x["original_weighted_receiver_potential_relative_l2"]<=1e-8
        assert 0<x["original_40_80_complex_pressure_transfer_relative_l2"]<=1e-8
        assert x["discrete_energy_relative_drift"]<=p["independent_solver"]["max_discrete_energy_relative_drift"]
        assert len(x["discrete_homogeneous_energy_probes"])==4
        assert [z["sample_n"] for z in x["discrete_homogeneous_energy_probes"]]==[
            1,x["original_native_record_sample_count"]//4,
            x["original_native_record_sample_count"]//2,
            x["original_native_record_sample_count"]-1]
        np.testing.assert_allclose(unpairs(x["native_exact_original_P_T_over_Q_T_40_80"]),
            unpairs(wv["unmodified_original_transfer_pa_per_m3_s"]),rtol=1e-10,atol=1e-8)
        np.testing.assert_allclose(unpairs(x["independent_full_state_CSR_P_T_over_Q_T_40_80"]),
            unpairs(x["native_exact_original_P_T_over_Q_T_40_80"]),rtol=1e-8,atol=1e-8)

def test_independent_sparse_csr_fully_constrained_native_original_time_recurrence():
    # Closed 4-node graph, duplicated source/receiver indices mimic native
    # 8-node point interpolation exactly, without evaluating upstream kernels.
    A=sparse.csr_matrix([[1.,-1.,0,0],[-1.,2.,-1.,0],
                         [0,-1.,2.,-1.],[0,0,-1.,1.]])
    source_ix=np.array([0,1,1,2,2,0,1,2])
    weights=np.array([.2,.11,.04,.06,.09,.1,.2,.2])
    recv_ix=np.array([1,3,2,0,1,2,3,3])
    l2=.2;dt=.001;nt=180
    predicted,probes,drift,secs=independent_native_csr_leapfrog(
        A,dt,nt,l2,source_ix,weights,recv_ix,[1,nt//4,nt//2,nt-1])
    prev=np.zeros(4);current=np.zeros(4)
    forcing=np.zeros(4)
    np.add.at(forcing,source_ix,weights)
    true=np.empty((8,nt))
    for step in range(nt):
        true[:,step]=current[recv_ix]
        following=2*current-prev-l2*(A.toarray()@current)
        if step==0:following+=forcing
        prev,current=current,following
    np.testing.assert_allclose(predicted,true,rtol=1e-10,atol=1e-12)
    assert np.max(np.abs(predicted[:,0]))==0
    assert drift<1e-10
    assert all(z["native_discrete_energy"]>0 for z in probes)
    assert secs>=0

def test_signed_complex_relative_metric_is_phase_sensitive():
    ref=np.array([1+2j,3-4j])
    assert relative_l2(ref,ref)==0
    assert relative_l2(-ref,ref)==pytest.approx(2)
    with pytest.raises(ValueError):
        relative_l2(np.array([np.inf,0]),ref)
