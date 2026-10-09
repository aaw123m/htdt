"""True 3D P1 Galerkin exact physical mass, original 8node q0 full evidence."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import linalg

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    generalized_symmetric_P1_consistent_modes,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_q0_conforming_p1_consistent_mass import (
    validate_plan,ARMS)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json"
PRIOR=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_multigrid_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


def physical_synthetic_mesh():
    a=np.arange(-.2,4.201,.2)
    return build_original_native_conforming_roof_p1(
        [a,a,a],max_yz_nodes=1000,max_3d_nodes=40000)


def test_true_consistent_mass_manufactured_exact_integral_x_plus_z_squared():
    fem=physical_synthetic_mesh()
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(fem)
    M,K=true_full_3d_consistent_P1_MK(mx,my,kx,ky)
    nl=len(fem.x_positions_m);ny=len(fem.yz_positions_m)
    assert M.shape==(nl*ny,nl*ny)
    assert mx.nnz>nl and my.nnz>ny and M.nnz>nl*ny
    assert np.allclose(np.asarray(mx.sum(axis=1)).ravel(),fem.x_mass,
                       rtol=2e-12,atol=1e-12)
    assert np.allclose(np.asarray(my.sum(axis=1)).ravel(),fem.yz_mass,
                       rtol=2e-12,atol=1e-12)
    assert abs(np.sum(M@np.ones(nl*ny))-56)<2e-10
    assert np.all(M.diagonal()>0)
    # Physical exact integral of (x+z)^2 over
    # 0<=x,y<=4,0<=z<=4-.25*y is 2780/3 m^5.
    u=(fem.x_positions_m[:,None]+fem.yz_positions_m[None,:,1]).ravel()
    exact_mass=2780/3
    actual=float(u@(M@u))
    assert abs(actual-exact_mass)/exact_mass<2e-10
    # The exact P1 gradient is (1,0,1); true Galerkin
    # energy ∫c² |grad u|² = 2*c²*56.
    expected_energy=112*343.2**2
    energy=float(u@(K@u))
    assert abs(energy-expected_energy)/expected_energy<2e-10
    # A lumped matrix is not an exact consistent mass for squared affines.
    old_m,_=fem.sparse_full_3d_operators()
    assert float(u@(old_m@u))>exact_mass


def test_complete_mass_orthogonal_true_P1_modes_no_clipped_eigenvalues():
    fem=physical_synthetic_mesh()
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(fem)
    for M,K in ((mx,kx),(my,ky)):
        eigen,v,info=generalized_symmetric_P1_consistent_modes(M,K)
        assert len(eigen)==M.shape[0]
        assert eigen[0]==0 and np.all(np.diff(eigen)>=-1e-8)
        assert info["all_physical_consistent_mass_generalized_modes_retained"]
        assert info["exact_rigid_neumann_constant_mode"]
        assert info["strict_max_all_eigenpair_residual"]<2e-7
        assert info["strict_max_M_orthogonality_residual"]<5e-8
        assert abs(np.sum(M@np.ones(M.shape[0]))-M.sum())<1e-10


def test_true_consistent_physical_FEM_preregistered_source_and_original_three_gates():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["frozen"]["ppw"]==list(PPW)
    assert p["frozen"]["physical_source_xyz"]==[1.5,2,2]
    assert p["frozen"]["physical_receiver_xyz"]==[2.5,2,2]
    assert p["frozen"]["original_q0_sample_one_then_zero"]
    assert p["frozen"]["record_s"]==.25
    assert p["frozen"]["frequency_hz"]==[40,80]
    assert p["limits"]["max_yz_vertices"]==3000
    assert p["limits"]["new_upstream_PFFDTD_wave_runs"]==0
    assert p["limits"]["new_github_actions_runs"]==0


@pytest.mark.parametrize("key,new",[
    ("original_complex_gate",1),
    ("original_magnitude_gate",3),
    ("original_phase_deg_gate",180)])
def test_true_consistent_mass_frozen_gate_relaxation_rejected(key,new):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["verification"][key]=new
    with pytest.raises(ValueError,match="preregistration drift"):
        validate_plan(p)


def test_real_all_five_original_q0_consistent_P1_mass_complete_negative_evidence():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    prev=json.loads(PRIOR.read_text(encoding="utf-8"))
    original=json.loads(RAW.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["prospective_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_native_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["external_physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_upstream_pffdtd_waves"]==e["new_github_actions_runs"]==0
    assert e["preobserved_original_point_q0_lumped_FEM_baseline_frozen"]
    cases=e["actual_conforming_P1_consistent_mass_original_q0_cases"]
    pre={z["ppw"]:z for z in prev["actual_original_q0_conforming_roof_P1_FEM_cases"]}
    raw={z["ppw"]:z for z in original["actual_native_wave_cases"]}
    assert [x["ppw"] for x in cases]==list(PPW)
    assert sum(z["all_true_nonfiltered_consistent_FEM_3D_modes"] for z in cases)==401630
    for z in cases:
        ppw=z["ppw"]
        assert z["source_original_comm_sha256"]==raw[ppw]["original_native_comm_sha256"]
        assert z["geometry_original_voxel_sha256"]==raw[ppw]["original_solver_geometry_sha256"]
        assert z["native_Ts_s"]==pre[ppw]["original_native_Ts_s"]
        assert z["native_Nt"]==pre[ppw]["original_native_Nt"]
        assert z["all_true_nonfiltered_consistent_FEM_3D_modes"]==pre[ppw][
            "full_original_native_grid_spanning_all_FEM_modes"]
        assert z["physical_true_FEM_yz_triangles"]==pre[ppw]["true_FEM_yz_triangles"]
        assert z["all_original_eight_point_q0_causal_full_record_unchanged"]
        assert z["all_added_mesh_roof_boundary_nodes_have_zero_original_q0_load"]
        assert z["all_high_frequency_physical_modes_retained"]
        assert abs(z["consistent_full_3D_mass_volume_m3"]-56)<2e-8
        assert z["consistent_true_3D_mass_nnz"]>z["all_true_nonfiltered_consistent_FEM_3D_modes"]
        assert z["strict_true_3D_FEM_eigenpair_residual"]<2e-7
        assert z["strict_true_3D_Neumann_rigid_zero_residual"]<1e-10
        for ax in ("x","yz"):
            check=z["all_true_M_consistent_P1_"+ax+"_modes_check"]
            assert check["strict_max_all_eigenpair_residual"]<2e-7
            assert check["strict_max_M_orthogonality_residual"]<5e-8
        old=unpairs(z["preobserved_full_lumped_P1_250ms_original_q0_signed_40_80"])
        previous=unpairs(pre[ppw]["new_FEM_full_250ms_original_point_q0_signed_40_80"])
        np.testing.assert_allclose(old,previous,rtol=1e-11,atol=1e-8)
        assert np.isfinite(unpairs(z["new_full_consistent_P1_250ms_original_q0_signed_40_80"])).all()
    compared=e["full_frozen_40_80_adjacent"]
    assert [(z["coarse_ppw"],z["fine_ppw"]) for z in compared]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,q in enumerate(compared):
        assert set(q["arms"])==set(ARMS)
        for arm,key in (
            ("preobserved_P1_lumped_mass","preobserved_full_lumped_P1_250ms_original_q0_signed_40_80"),
            ("new_P1_true_consistent_mass","new_full_consistent_P1_250ms_original_q0_signed_40_80")):
            c=cases[j][key];f=cases[j+1][key]
            score=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            record=q["arms"][arm]
            for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(record["full_frozen_original_complex_mag_phase"][k]-score[k])<1e-11
            assert np.allclose(unpairs(record["true_original_two_bin_signed_coarse_minus_fine"]),
                               unpairs(c)-unpairs(f),rtol=1e-11,atol=1e-8)
            passed=(score["complex_rms_relative"]<=.2 and
                    score["magnitude_max_relative"]<=.25 and
                    score["phase_max_deg"]<=15)
            assert passed is record["original_three_gates_all_pass"]
    # This one exceptional full 3-gate pass at PPW32->36 is RETAINED,
    # not extrapolated or promoted to a five-grid convergence claim.
    assert compared[1]["arms"]["new_P1_true_consistent_mass"]["original_three_gates_all_pass"]
    assert all(not compared[j]["arms"]["new_P1_true_consistent_mass"]["original_three_gates_all_pass"]
               for j in (0,2,3))
    for v in e["all_five_original_q0_consistent_mass_comparison_verdicts"].values():
        assert not v["all_four_pairs_original_three_gate_pass"]
        assert not v["strict_three_metric_monotonicity"]
        assert v["cannot_requalify_original_upstream_PFFDTD"]
