"""Original native PFFDTD non-promotable modal spectrum and exact q0 transfer."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_native_modal_drift import (
    PPW,PIN,FREQ,validate_plan,native_q0_modal_approximation,
    pairs,unpairs)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_native_modal_drift_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_native_modal_drift_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_original_fullband_8node_model_is_unmodified_and_not_promoted():
    p,d=frozen()
    assert p["source_authority"]["original_ppw"]==list(PPW)
    assert p["source_authority"]["original_full_transfer_frequencies_hz"]==[40,80]
    assert p["spectral_problem"]["number_eigenpairs"]==20
    assert p["limits"]["run_locally_only_no_github_actions"] is True
    assert p["source_authority"]["pffdtd_upstream_git_sha"]==PIN
    assert p["release"]["original_canonical_point_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert d["pinned_exact_upstream_sha"]==PIN
    assert d["original_q0_fullband_state"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"
    assert d["diagnostic_only_did_not_rerun_or_change_original_native_wave"] is True

@pytest.mark.parametrize("mutation",[
    lambda p:p["source_authority"].update(original_ppw=[28,32]),
    lambda p:p["source_authority"].update(physical_source_xyz_m=[1.5,2.1,2]),
    lambda p:p["source_authority"].update(original_full_transfer_frequencies_hz=[50,80]),
    lambda p:p["spectral_problem"].update(number_eigenpairs=4),
    lambda p:p["limits"].update(run_locally_only_no_github_actions=False),
    lambda p:p["release"].update(production="GO"),
])
def test_original_modal_plan_rejects_posthoc_changes(mutation):
    p,_=frozen()
    mutation(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_all_original_five_native_graph_eigenvalues_and_modal_overlap_integrity():
    p,d=frozen()
    graph=json.loads((ROOT/p["source_authority"]["previous_original_graph_evidence"]).read_text())
    waves=json.loads((ROOT/p["source_authority"]["previous_original_wave_evidence"]).read_text())
    rows=d["actual_original_native_modal_levels"]
    assert [r["ppw"] for r in rows]==list(PPW)
    for row,g,w in zip(rows,graph["actual_original_voxel_grid_audits"],waves["actual_native_wave_cases"]):
        assert row["ppw"]==g["ppw"]==w["ppw"]
        assert row["exact_original_native_voxel_sha256"]==g["original_exact_voxel_sha256"]
        assert row["exact_original_native_comms_sha256"]==w["original_native_comm_sha256"]
        assert row["native_room_reachable_vertices"]==g["source_connected_room_nodes"]
        assert row["original_full_native_signed_40_80"]==w["unmodified_original_transfer_pa_per_m3_s"]
        assert row["eigenpair_count"]==len(row["eigenmodes"])==20
        assert row["max_true_relative_eigen_residual"]<=p["limits"]["max_eigen_relative_residual"]
        assert row["native_cfl_squared"]<1/3
        assert row["q0_and_spatial_trilinear_source_receiver_unchanged"] is True
        assert row["all_original_rigid_boundary_neumann_edges_unchanged"] is True
        assert row["low_modes_diagnostic_not_full_band_qualification"] is True
        modes=row["eigenmodes"]
        assert [z["mode_index"] for z in modes]==list(range(20))
        fnative=np.array([z["frequency_native_leapfrog_hz"] for z in modes])
        assert np.all(np.diff(fnative)>=-1e-10)
        assert fnative[0]<.005
        assert np.all(np.isfinite(fnative))
        expected_theta=2*np.arcsin(np.sqrt(np.maximum([z["dimensionless_laplacian_lambda"] for z in modes],0)*row["native_cfl_squared"])/2)
        np.testing.assert_allclose(fnative,expected_theta/(2*np.pi*row["native_dt_s"]),rtol=2e-12,atol=2e-8)
        assert max(z["true_original_neumann_eigen_relative_residual"] for z in modes)<=5e-7
        signed_sum=np.sum([unpairs(z["finite_250ms_signed_original_q0_pressure_transfer_40_80"]) for z in modes],axis=0)
        np.testing.assert_allclose(signed_sum,unpairs(row["first_20_modal_signed_40_80"]),rtol=3e-11,atol=2e-8)
        full=unpairs(row["original_full_native_signed_40_80"])
        observed=float(np.linalg.norm(signed_sum-full)/np.linalg.norm(full))
        assert row["lowmode_vs_original_full_native_complex_relative_norm"]==pytest.approx(
            observed,rel=2e-11,abs=2e-10)
        for item,f in zip(row["nearest_to_40_80_hz"],FREQ):
            i=int(np.argmin(abs(fnative-f)))
            assert item["nearest_native_mode_index"]==i
            assert item["nearest_native_eigenfrequency_hz"]==pytest.approx(fnative[i],rel=2e-12,abs=1e-8)
            assert item["native_mode_frequency_offset_from_target_hz"]==pytest.approx(fnative[i]-f,rel=2e-12,abs=1e-8)
    assert len(d["same_mode_index_native_frequency_hz_drift_from_ppw44"])==5
    assert all(len(x["frequencies_k1_to_k19_delta_hz"])==19
           for x in d["same_mode_index_native_frequency_hz_drift_from_ppw44"])
    assert any(z["lowmode_vs_original_full_native_complex_relative_norm"]>.5 for z in rows)
    assert d["original_q0_fullband_state"]=="SELF_CONVERGENCE_FAILED"

def test_full_mode_projection_equals_independently_integrated_native_leapfrog_exact_q0():
    # A 4-node Neumann chain gives a full orthonormal eigenbasis; compare
    # against the direct discrete original FDTD n-loop without using spectral
    # recurrence, including the native post-update source injection sample.
    n=4
    A=np.array([[1.,-1.,0.,0.],[-1.,2.,-1.,0.],
                [0.,-1.,2.,-1.],[0.,0.,-1.,1.]])
    lam,V=np.linalg.eigh(A)
    dt=.002;h=1.;c=40.;rho=1.2;nt=125
    source_ix=np.array([0,1],dtype=int);source_w=np.array([.4,.6])
    recv_ix=np.array([2,3],dtype=int);recv_w=np.array([.7,.3])
    scale=.031
    angles,each,total,coupling=native_q0_modal_approximation(
        lam,V,source_ix,source_w,recv_ix,recv_w,scale,dt,h,c,rho,nt)
    x_prev=np.zeros(n);x_current=np.zeros(n)
    phi=[]
    forcing=np.zeros(n)
    np.add.at(forcing,source_ix,source_w*scale)
    for step in range(nt):
        phi.append(np.dot(recv_w,x_current[recv_ix]))
        x_new=2*x_current-x_prev-(c*dt/h)**2*(A@x_current)
        if step==0:x_new+=forcing
        x_prev,x_current=x_current,x_new
    from htdt.acoustic_pffdtd_adapter import (
        pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
    pressure=pffdtd_velocity_potential_to_pressure_trace(np.asarray(phi),time_step_s=dt,density_kg_m3=rho)
    q=np.zeros(nt);q[0]=1
    direct=finite_record_pressure_transfer(pressure,q,time_step_s=dt,frequency_hz=FREQ)
    np.testing.assert_allclose(total,direct,rtol=2e-10,atol=1e-8)
    np.testing.assert_allclose(total,np.sum(each,axis=0),rtol=1e-12,atol=1e-10)
    assert abs(phi[0])<1e-14
    assert np.max(abs(each))>0
