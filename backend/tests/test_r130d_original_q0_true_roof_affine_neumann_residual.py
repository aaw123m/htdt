"""Original q0 HDF5 true-roof harmonic boundary consistency, no gate changes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz)
from htdt.r130d_true_roof_manufactured_neumann import (
    SLOPE,TRUE_NORMAL,TRUE_TANGENT,
    true_roof_affine_local_neumann_residual)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_true_roof_affine_neumann_residual import validate_plan
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_oblique_neumann_manufactured_affine_residual_plan_2026-10-10.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_oblique_neumann_manufactured_affine_residual_evidence_2026-10-10.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
ROOF=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json"
Q1=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"


def synthetic_true_roof_native_and_Q1():
    h=.2
    a=np.arange(-h,4+h+.0001,h)
    nx,ny,nz=5,len(a),len(a)
    roof_yz=cut_roof_native_original_Q1_galerkin_yz(a,a,max_active_yz_nodes=1100)
    # Controlled original-style source-free three x-layer Neumann staircase
    # with +z blocked only when crossing the TRUE physical sloped roof.
    ids=[]
    adjacency=[]
    for ix in (1,2,3):
        for iy in range(3,ny-3):
            y=float(a[iy])
            if not 0<y<4:continue
            for iz in range(3,nz-3):
                z=float(a[iz])
                if z>4-.25*y+1e-10:continue
                if z<=4-.25*y-2*h:continue
                a6=np.ones(6,dtype=np.int64)
                if iz+1<nz and float(a[iz+1])>4-.25*y+1e-10:
                    a6[4]=0
                if iy+1<ny and z>4-.25*float(a[iy+1])+1e-10:
                    a6[2]=0
                if a6[2]==0 or a6[4]==0:
                    ids.append(ix*ny*nz+iy*nz+iz)
                    adjacency.append(a6)
    return (a,roof_yz,np.asarray(ids,dtype=np.int64),
            np.asarray(adjacency,dtype=np.int64),(nx,ny,nz))


def test_true_roof_tangent_is_exact_harmonic_neumann():
    assert SLOPE==.25
    assert abs(float(TRUE_NORMAL@TRUE_TANGENT))<1e-15
    assert np.isclose(np.linalg.norm(TRUE_NORMAL),1.)
    # Independent direct Cartesian Laplacian finite difference should
    # vanish in the interior, rather than merely being asserted.
    h=.013
    u=lambda y,z:y-.25*z
    for y,z in ((.5,2.5),(2.,1.),(3.,2.)):
        lap=(u(y+h,z)+u(y-h,z)+u(y,z+h)+u(y,z-h)-4*u(y,z))/h**2
        assert abs(lap)<5e-12


def test_synthetic_native_exact_stair_discrete_action_nonzero_true_Q1_weak_zero():
    a,q,bn,adj,dims=synthetic_true_roof_native_and_Q1()
    assert len(bn)>=30
    got=true_roof_affine_local_neumann_residual(a,a,bn,adj,dims,q)
    assert got["actual_original_native_genuine_roof_stencil_rows"]>=30
    assert got["original_strong_laplacian_affine_harmonic_residual_RMS_inverse_m"]>.5
    assert got["original_h_times_strong_laplacian_roof_residual_RMS_dimensionless"]>.1
    assert got["true_cut_Q1_roof_only_affine_weak_K_u_peak_in_Ku_units"]<1e-7
    assert got["original_graph_strong_residual_and_true_Q1_weak_residual_have_DIFFERENT_UNITS"]
    assert got["native_original_strong_action_independent_direct_sixneighbor_max_difference"]<1e-9


@pytest.mark.parametrize("bad",["invalid_adjacency","invalid_grid_shape","empty_roof"])
def test_fail_closed_real_native_adj_data_source(bad):
    a,q,bn,adj,dims=synthetic_true_roof_native_and_Q1()
    if bad=="invalid_adjacency":
        adj=adj.copy();adj[0,4]=9
    elif bad=="invalid_grid_shape":
        dims=(dims[0],dims[1]+1,dims[2])
    else:
        adj=np.ones_like(adj)
    with pytest.raises(ValueError):
        true_roof_affine_local_neumann_residual(a,a,bn,adj,dims,q)


@pytest.mark.parametrize("edit",[
    ("original","unchanged_full_250ms_q0_40_80_signed",False),
    ("original","original_40_80_full250ms_frozen_gates",[2,2,40]),
    ("manufactured","original_native_6direction_adjacency_order",
      ["-x","+x","+y","-y","+z","-z"])])
def test_plan_fails_closed_on_q0_scope_or_native_direction_change(edit):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    part,key,val=edit
    p[part][key]=val
    with pytest.raises(ValueError,match="preregistered physical roof"):
        validate_plan(p)


def test_original_native_true_roof_manufactured_actual_five_ppw_hdf5_evidence_and_failed_250ms():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    original=json.loads(RAW.read_text(encoding="utf-8"))
    roof=json.loads(ROOF.read_text(encoding="utf-8"))
    q1=json.loads(Q1.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["canonical_original_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_PFFDTD_runs"]==e["new_GitHub_actions"]==0
    assert e["original_q0_full250ms_still_all4_nonconvergent"]
    assert e["manufactured_strong_and_weak_residual_UNITS_not_comparable"]
    assert e["not_proof_sole_causing_original_250ms_nonconvergence"]
    rows=e["actual_original_genuine_6neigh_vs_true_roof_Q1_five_grid_manufactured"]
    assert [r["ppw"] for r in rows]==list(PPW)
    old={r["ppw"]:r for r in original["actual_native_wave_cases"]}
    echo={r["ppw"]:r for r in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    fem={r["ppw"]:r for r in q1["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    for row in rows:
        ppw=row["ppw"]
        assert row["source_HDF5_sha256"]==old[ppw]["original_native_comm_sha256"]
        assert row["room_HDF5_sha256"]==old[ppw]["original_solver_geometry_sha256"]
        assert row["original_full_wave_HDF5_sha256"]==echo[ppw][
            "original_true_native_entire_real_q0_HDF5_SHA256"]
        assert row["canonical_real_wave_vs_historical_relative"]<2e-6
        assert row["original_native_dt_s"]==fem[ppw]["original_native_Ts_s"]
        assert row["original_Nt"]==fem[ppw]["original_native_full_Nt"]
        assert row["physical_true_Q1_all_positive_support_yz_basis_modes"]==fem[ppw][
            "real_original_native_cartesian_yz_Q1_physical_support_modes"]
        assert abs(row["physical_true_room_volume_m3"]-56)<2e-8
        assert row["original_true_wave_and_q0_source_untouched"]
        np.testing.assert_allclose(
            unpairs(row["unchanged_original_250ms_signed_40_80_P_T_over_Q_T"]),
            unpairs(old[ppw]["unmodified_original_transfer_pa_per_m3_s"]),
            rtol=2e-6,atol=1e-5)
        n=row["original_real_PFFDTD_vs_true_neumann_Q1_affine_roof_manufactured"]
        assert n["actual_original_native_genuine_roof_stencil_rows"]>100
        assert n["physical_true_cut_Q1_unique_roof_only_basis_rows"]>5
        assert n["original_real_roof_stencil_z_crossings"]>0
        assert n["original_real_roof_stencil_y_crossings"]>0
        assert n["original_strong_laplacian_affine_harmonic_residual_RMS_inverse_m"]>3
        assert .4<n["original_h_times_strong_laplacian_roof_residual_RMS_dimensionless"]<.5
        assert n["true_cut_Q1_roof_only_affine_weak_K_u_peak_in_Ku_units"]<3e-9
        assert n["true_cut_Q1_roof_only_affine_weak_K_u_peak_relative_to_global_K_diagonal"]<2e-12
        assert n["native_original_strong_action_independent_direct_sixneighbor_max_difference"]<1e-9
        assert abs(n["physical_true_roof_affine_exact_normal_derivative"])<1e-15
        assert n["original_graph_strong_residual_and_true_Q1_weak_residual_have_DIFFERENT_UNITS"]
        assert n["do_NOT_ascribe_entire_250ms_q0_failure_solely_to_this_boundary_example"]
    # The dimensionless h*residual remains O(1), rather than improving
    # under true native grid refinement. This is the essential boundary
    # consistency finding; compare within SAME discretization/units only.
    rms=[r["original_real_PFFDTD_vs_true_neumann_Q1_affine_roof_manufactured"][
        "original_strong_laplacian_affine_harmonic_residual_RMS_inverse_m"]
        for r in rows]
    assert all(b>a for a,b in zip(rms,rms[1:]))
    checks=e["unchanged_original_full250ms_signed_4adjacent_gate_scores"]
    assert [(v["coarse_ppw"],v["fine_ppw"]) for v in checks]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,metric in enumerate(checks):
        s=compare_complex_transfer(
            candidate=rows[j]["unchanged_original_250ms_signed_40_80_P_T_over_Q_T"],
            reference=rows[j+1]["unchanged_original_250ms_signed_40_80_P_T_over_Q_T"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        for name in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(metric["real_original_full250ms_canonical_signed_three_metrics"][name]-s[name])<1e-10
        assert not metric["all_original_three_gates_pass"]
