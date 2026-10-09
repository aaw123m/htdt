"""Original UNMODIFIED PFFDTD 8-node q0 full native-mode finite signed transfer.

Evidence is original FDTD raw HDF5 pinned by SHA; no product promotion, and
each original failure is retained and rechecked against true full wave.
"""
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
from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer,pffdtd_velocity_potential_to_pressure_trace)
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    PPW,BANDS,FREQ,validate_plan,graph_from_original_6_neighbors_ppw,
    true_native_leapfrog_exact_finite_signed_transfer,unpairs)
from htdt.r130d_general3d_validation import compare_complex_transfer

PLAN=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_kronecker_modal_q0_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_all_five_unmodified_source_q0_native_PFFDTD_full_all_modes_original_failure_preserved():
    p,d=frozen()
    assert p["original_authority"]["frozen_original_native_ppw"]==list(PPW)
    assert p["original_authority"]["original_source_xyz_m"]==[1.5,2,2]
    assert p["original_authority"]["original_receiver_xyz_m"]==[2.5,2,2]
    assert p["original_authority"]["original_full_no_taper_record_s"]==.25
    assert p["original_authority"]["frequencies_hz"]==[40,80]
    assert p["spatial"]["matrix_max_absolute_difference"]==0
    assert p["limits"]["no_github_actions_runs"] is True
    assert d["new_GitHub_Actions_launched"]==0
    assert d["upstream_PIN"]==p["original_authority"]["pffdtd_sha"]
    assert d["canonical_original_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"
    assert d["actual_original_point_q0_full_wave_acceptance_still_failed"] is True
    assert d["original_PFFDTD_native_source_solver_HDF5_unmodified"] is True

@pytest.mark.parametrize("key,value",[
    ("frozen_original_native_ppw",[28,32]),
    ("original_source_xyz_m",[1.4,2,2]),
    ("original_receiver_xyz_m",[2.6,2,2]),
    ("frequencies_hz",[40]),
    ("original_full_no_taper_record_s",.15)])
def test_exact_original_PFFDTD_modal_plan_physical_mutations_fail_closed(key,value):
    p,_=frozen();p["original_authority"][key]=value
    with pytest.raises(ValueError):validate_plan(p)

def test_original_acceptance_threshold_or_time_sampling_edit_fails_closed():
    p,_=frozen();p["bins"]["original_frozen_complex_threshold"]=1
    with pytest.raises(ValueError):validate_plan(p)
    p,_=frozen();p["bins"]["semidiscrete_frequencies_hz"]=[[0,80],[80,1000000000]]
    with pytest.raises(ValueError):validate_plan(p)
    p,_=frozen();p["release"]["product"]="GO"
    with pytest.raises(ValueError):validate_plan(p)

@pytest.mark.parametrize("theta",[0.,1e-7,.2,.91,1.8,2.9,3.13])
def test_exact_original_finite_q0_leapfrog_modal_fourier_vs_direct_time_update(theta):
    nt=731;dt=.00025;rho=1.2;amplitude=np.array([.051])
    n=np.arange(nt)
    phi=amplitude[0]*(n if theta==0 else np.sin(n*theta)/np.sin(theta))
    pressure=pffdtd_velocity_potential_to_pressure_trace(phi,time_step_s=dt,density_kg_m3=rho)
    src=np.zeros(nt);src[0]=1.
    original=finite_record_pressure_transfer(
        pressure,src,time_step_s=dt,frequency_hz=FREQ)
    analyt=true_native_leapfrog_exact_finite_signed_transfer(
        np.array([theta]),amplitude,dt,nt,rho)[:,0]
    np.testing.assert_allclose(analyt,original,rtol=1e-8,atol=1e-6)

def test_synthetic_original_neumann_3D_graph_exact_x_yz_product_and_missing_vertex_fails():
    nx,ny,nz=4,5,4
    # Original all-connected Cartesian room from unmodified native graph rule.
    def chain(n):
        A=np.diag(np.r_[1.,np.full(n-2,2.),1.])
        for j in range(n-1):A[j,j+1]=A[j+1,j]=-1
        return sparse.csr_matrix(A)
    Ax=chain(nx)
    # yz graph on complete original 5x4 Cartesian grid.
    Ayz=sparse.kron(chain(ny),sparse.eye(nz,format="csr"))+sparse.kron(
        sparse.eye(ny,format="csr"),chain(nz))
    full=(sparse.kron(Ax,sparse.eye(ny*nz,format="csr"))+
        sparse.kron(sparse.eye(nx,format="csr"),Ayz)).tocsr()
    n=nx*ny*nz
    ordering=np.arange(n)[::-1]
    scrambled=full[ordering,:][:,ordering]
    Ax2,Ayz2,x,y,proof=graph_from_original_6_neighbors_ppw(
        scrambled,ordering,(nx,ny,nz))
    assert proof["original_full_native_staircase_A_equals_kron_Ax_I_plus_I_Ayz"] is True
    assert proof["original_native_Kronecker_neumann_matrix_max_abs"]==0
    assert proof["original_connected_nodes"]==n
    assert np.array_equal(Ax2.toarray(),Ax.toarray())
    assert np.array_equal(Ayz2.toarray(),Ayz.toarray())
    with pytest.raises(ValueError,match="not exact x"):
        graph_from_original_6_neighbors_ppw(
            scrambled[:-1,:-1],ordering[:-1],(nx,ny,nz))

def test_all_original_pffdtd_5_raw_native_8node_q0_modes_sha_and_true_signed_full_record():
    p,d=frozen()
    original=json.loads((ROOT/p["original_authority"]["raw_native_wave_SHA_evidence"]).read_text(encoding="utf-8"))
    graph=json.loads((ROOT/p["original_authority"]["native_true_rigid_graph_audit"]).read_text(encoding="utf-8"))
    partition=json.loads((ROOT/p["original_authority"]["native_original_high_ppw_signed_wave_partition"]).read_text(encoding="utf-8"))
    rows=d["actual_original_unmodified_all_mode_native_cases"]
    assert [r["ppw"] for r in rows]==list(PPW)
    total_modes=0
    for r,w,g,prior in zip(rows,original["actual_native_wave_cases"],
                          graph["actual_original_voxel_grid_audits"],partition["levels"]):
        assert r["ppw"]==w["ppw"]==g["ppw"]==prior["ppw"]
        assert r["original_native_comms_SHA256"]==w["original_native_comm_sha256"]
        assert r["original_native_voxels_SHA256"]==w["original_solver_geometry_sha256"]==g["original_exact_voxel_sha256"]
        assert r["original_native_sim_output_SHA256"]==prior["original_8node_native_output_sha256"]
        assert r["exact_original_connected_staircase_graph_tensor_identity"]["original_connected_nodes"]==g["source_connected_room_nodes"]
        assert r["exact_original_connected_staircase_graph_tensor_identity"]["original_directed_rigid_neumann_edges"]==g["connected_non_ghost_directed_edges"]
        assert r["exact_original_connected_staircase_graph_tensor_identity"]["original_native_Kronecker_neumann_matrix_max_abs"]==0
        assert r["exact_original_8point_source_tensor_residual"]<=1e-12
        assert r["exact_original_8point_receiver_tensor_residual"]<=1e-12
        assert r["actual_native_full_3D_modes_count"]==g["source_connected_room_nodes"]
        assert r["full_x_native_graph_modes"]*r["full_yz_native_original_staircase_graph_modes"]==r["actual_native_full_3D_modes_count"]
        assert r["true_full_eigenmode_max_absolute_Av_minus_lambda_v"]<3e-7
        assert r["original_native_const_neumann_zero_mode_present"] is True
        assert r["original_record_samples"]==w["native_solver"]["sample_count"]
        assert r["all_original_native_PFFDTD_modes_vs_original_saved_full_wave_complex_rel"]<p["solver"]["true_original_8node_all_raw_wave_matched_relative_max"]
        np.testing.assert_allclose(unpairs(r["entire_true_original_native_PFFDTD_3D_mode_sum_signed_40_80"]),
            unpairs(w["unmodified_original_transfer_pa_per_m3_s"]),rtol=2e-6,atol=5e-6)
        np.testing.assert_allclose(unpairs(r["exact_original_saved_true_PFFDTD_q0_signed_40_80"]),
            unpairs(prior["time_components"]["full_original_40_80_complex"]),rtol=1e-10,atol=2e-7)
        bands=r["all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer"]
        assert [x["semidiscrete_mode_frequency_band_hz"] for x in bands]==[list(b) for b in BANDS]
        assert sum(z["full_original_native_3D_mode_count"] for z in bands)==r["actual_native_full_3D_modes_count"]
        np.testing.assert_allclose(sum((unpairs(z["signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80"])
           for z in bands),np.zeros(2,dtype=complex)),unpairs(
                r["exact_original_saved_true_PFFDTD_q0_signed_40_80"]),rtol=2e-6,atol=4e-6)
        assert r["original_pffdtd_source_and_full250ms_40_80_unmodified"] is True
        assert r["original_native_modes_not_experimentally_damped_or_removed"] is True
        for label in ("40","80"):
            assert len(r["top20_original_native_impulse_coupled_3D_modes_each_40_80_bin"][label])==20
        total_modes+=r["actual_native_full_3D_modes_count"]
    assert total_modes==sum(q["source_connected_room_nodes"] for q in graph["actual_original_voxel_grid_audits"])

def test_all_original_4_PPW_adjacent_full_signed_mode_band_differences_and_original_FAILs():
    p,d=frozen()
    rows=d["actual_original_unmodified_all_mode_native_cases"]
    adjacent=d["all_four_actual_original_PFFDTD_adjacent_q0_mode_band_differences"]
    assert [(r["original_coarse_ppw"],r["original_fine_ppw"]) for r in adjacent]==[
        (28,32),(32,36),(36,40),(40,44)]
    for idx,r in enumerate(adjacent):
        total=(unpairs(rows[idx]["exact_original_saved_true_PFFDTD_q0_signed_40_80"])-
               unpairs(rows[idx+1]["exact_original_saved_true_PFFDTD_q0_signed_40_80"]))
        np.testing.assert_allclose(unpairs(r["original_full_250ms_signed_delta"]),total,rtol=3e-11,atol=2e-8)
        segments=r["original_full_signed_band_causal_differences"]
        assert [x["original_native_physical_semidiscrete_mode_band_hz"] for x in segments]==[list(y) for y in BANDS]
        reconstruction=sum((unpairs(z["original_signed_complex_delta_40_80"]) for z in segments),
                           np.zeros(2,dtype=complex))
        np.testing.assert_allclose(reconstruction,total,rtol=2e-6,atol=3e-5)
        for seg in segments:
            expected=float(np.linalg.norm(unpairs(seg["original_signed_complex_delta_40_80"]))/np.linalg.norm(total))
            assert seg["relative_complex_delta_norm_to_full_original"]==pytest.approx(expected,rel=1e-10)
        scored=compare_complex_transfer(
            reference=rows[idx+1]["exact_original_saved_true_PFFDTD_q0_signed_40_80"],
            candidate=rows[idx]["exact_original_saved_true_PFFDTD_q0_signed_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert r["original_full_native_PPWave_two_frequency_refinement"][key]==pytest.approx(
                scored[key],rel=5e-12,abs=5e-12)
        assert r["all_original_3D_mode_signed_reconstruction_conservation"] is True
    assert adjacent[2]["original_full_signed_band_causal_differences"][-1]["relative_complex_delta_norm_to_full_original"]>1
