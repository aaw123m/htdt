"""R130D original q0, actual oblique Neumann stair + nonfit 50:50 cut Q1.

Original all 5 SHA HDF5 waves, original actual 8node source and unchanged
250ms 40/80 signed gates. All-mode physical Q1 half-mass alternative FAIL.
"""
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
from htdt.r130d_oblique_roof_theory_blended_q1 import (
    PHYSICAL_ROOF_NORMAL,PHYSICAL_ROOF_TANGENT,
    wave_uniform_1d_symbol_ratio,half_Q1_true_roof_mass,
    full_theory_half_Q1_mass_and_true_roof_stiffness,
    inspect_original_staircase_true_roof_tangential_flux)
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import consistent_physical_P1_tensored_operators
from htdt.r130d_native_cut_roof_Q1_galerkin import cut_roof_native_original_Q1_galerkin_yz
from htdt.r130d_causal_first_roof_echo import ROOF_WIDTHS_S
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_oblique_roof_theory_half_Q1 import validate_plan
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_oblique_roof_and_fixed_half_Q1_mass_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_oblique_roof_theory_half_Q1_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
CONS=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"
LUMP=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_evidence_2026-10-09.json"
ROOF=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json"


def small_test():
    a=np.arange(-.2,4.201,.2)
    f=build_original_native_conforming_roof_p1(
        [a,a,a],max_yz_nodes=1000,max_3d_nodes=40000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(f)
    q=cut_roof_native_original_Q1_galerkin_yz(
        a,a,max_active_yz_nodes=1100)
    return f,q,mx,kx


def test_affine_true_roof_tangential_neumann_normal_zero_but_stair_axis_nonzero():
    assert PHYSICAL_ROOF_NORMAL.shape==(2,)
    assert abs(float(PHYSICAL_ROOF_NORMAL@PHYSICAL_ROOF_TANGENT))<1e-15
    assert abs(float(np.linalg.norm(PHYSICAL_ROOF_NORMAL))-1)<1e-15
    assert abs(PHYSICAL_ROOF_TANGENT[0])==1.
    assert abs(PHYSICAL_ROOF_TANGENT[1])==.25
    # Exact true cut Q1 interpolation of u=y−.25z has this same gradient
    # everywhere, including the geometrically clipped roof.
    for y in np.linspace(.25,3.75,8):
        z=4-.25*y
        exact_u=y-.25*z
        n_flux=PHYSICAL_ROOF_NORMAL@PHYSICAL_ROOF_TANGENT
        assert abs(n_flux)<1e-15 and np.isfinite(exact_u)


@pytest.mark.parametrize("kh",[(0.4,0.2),(0.2,0.1),(.1,.05)])
def test_nonfit_analytic_half_mass_cancels_uniform_1d_O_h2_dispersion(kh):
    v=np.array(kh,dtype=float)
    ehalf=abs(wave_uniform_1d_symbol_ratio(v,mass_kind="half")-1)
    elump=abs(wave_uniform_1d_symbol_ratio(v,mass_kind="lumped")-1)
    econs=abs(wave_uniform_1d_symbol_ratio(v,mass_kind="consistent")-1)
    assert ehalf[0]>0 and ehalf[1]>0
    assert 14<ehalf[0]/ehalf[1]<18.5  # leading fourth order when kh halves
    assert 3.7<elump[0]/elump[1]<4.2
    assert 3.7<econs[0]/econs[1]<4.3
    assert max(ehalf)<max(elump) and max(ehalf)<max(econs)
    with pytest.raises(ValueError,match="unknown theoretical"):
        wave_uniform_1d_symbol_ratio(v,mass_kind="fitted")


def test_positive_half_mass_true_cut_roof_no_sliver_floors_and_neumann_56m3():
    f,q,mx,kx=small_test()
    half=half_Q1_true_roof_mass(mx,q.mass,kx,q.stiffness)
    m,k=full_theory_half_Q1_mass_and_true_roof_stiffness(half)
    n=len(f.x_positions_m)*q.native_yz_modes
    assert half.mode_count==n and m.shape==(n,n)
    assert abs(float(m.sum())-56)<2e-8
    assert np.min(m.diagonal())>0
    assert m.nnz>n
    assert q.occupied_cartesian_original_exterior_nodes>0
    assert q.minimum_strict_positive_cut_polygon_m2>0
    np.testing.assert_allclose(half.Mx@np.ones(len(f.x_positions_m)),
                               mx@np.ones(len(f.x_positions_m)),atol=1e-12)
    np.testing.assert_allclose(half.Myz@np.ones(q.native_yz_modes),
                               q.mass@np.ones(q.native_yz_modes),atol=1e-12)
    assert np.min(half.yz_true_mass_rows)>0
    assert np.min(half.x_true_mass_rows)>0
    assert max(abs(k@np.ones(n)))/max(1,max(abs(k.diagonal())))<1e-10
    z=q.physical_active_yz_node_positions_m[:,1]
    u=(f.x_positions_m[:,None]+z[None,:]).ravel()
    assert abs(float(u@(k@u))/(112*343.2**2)-1)<2e-9
    # Strict SPD + true Neumann eigenvalue checks on small physical sections,
    # and ALL small-section mode count retained.
    vals=linalg.eigvalsh(half.Kyz.toarray(),half.Myz.toarray())
    assert len(vals)==q.native_yz_modes and vals[1]>0
    assert abs(vals[0])/max(abs(vals[-1]),1)<1e-12


def test_invalid_physical_mass_fails_without_flooring_or_ghost_clipping():
    f,q,mx,kx=small_test()
    with pytest.raises(ValueError,match="true roof tiny mass"):
        half_Q1_true_roof_mass(mx,-q.mass,kx,q.stiffness)
    with pytest.raises(ValueError,match="invalid exact physical"):
        half_Q1_true_roof_mass(mx[:3,:3],q.mass,kx,q.stiffness)


@pytest.mark.parametrize("name,value",[
    ("complex_rms_gate",1.),
    ("magnitude_max_gate",9.),
    ("phase_max_deg_gate",90.)])
def test_original_frozen_40_80_gate_cannot_change(name,value):
    plan=json.loads(PLAN.read_text(encoding="utf-8"))
    plan["frozen_original"][name]=value
    with pytest.raises(ValueError,match="preregistration drift"):
        validate_plan(plan)


def test_all_original_SHA_hdf5_5grid_oblique_true_roof_geometry_and_theory_half_FAIL():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    evidence=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    raw=json.loads(RAW.read_text(encoding="utf-8"))
    consistent=json.loads(CONS.read_text(encoding="utf-8"))
    lumped=json.loads(LUMP.read_text(encoding="utf-8"))
    roof=json.loads(ROOF.read_text(encoding="utf-8"))
    assert evidence["precommitted_plan"]==p
    assert evidence["frozen_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert evidence["unchanged_original_PFFDTD_self_convergence"]=="SELF_CONVERGENCE_FAILED"
    assert evidence["independent_physical_validation"]=="NOT_VALIDATED"
    assert evidence["product"]=="NO_GO"
    assert evidence["new_original_native_PFFDTD_runs"]==evidence["new_GitHub_Actions_runs"]==0
    assert evidence["no_production_go_or_independent_physical_validation"]
    real={q["ppw"]:q for q in raw["actual_native_wave_cases"]}
    con={q["ppw"]:q for q in consistent["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    lump={q["ppw"]:q for q in lumped["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]}
    origroof={q["ppw"]:q for q in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    rows=evidence["actual_all_five_original_native_q0_theory_half_cut_Q1_cases"]
    assert [q["ppw"] for q in rows]==list(PPW)
    assert sum(q["true_original_cut_Q1_physical_full_3D_nodes_all_modes"] for q in rows)==401630
    for r in rows:
        k=r["ppw"]
        assert r["original_SHA256_comms"]==real[k]["original_native_comm_sha256"]
        assert r["original_SHA256_voxel"]==real[k]["original_solver_geometry_sha256"]
        assert r["original_SHA256_full_native_real_wave"]==origroof[k][
            "original_true_native_entire_real_q0_HDF5_SHA256"]
        assert r["native_original_Ts_s"]==con[k]["original_native_Ts_s"]
        assert r["native_original_Nt"]==con[k]["original_native_full_Nt"]
        assert r["true_original_cut_Q1_physical_full_3D_nodes_all_modes"]==con[k][
            "real_original_native_3D_full_true_Q1_modes"]
        assert abs(r["physical_exact_3D_volume_m3"]-56)<2e-8
        assert r["positive_yz_cut_Q1_lump_min_mass_m2"]>0
        assert r["original_true_positive_outside_support_ghost_nodes_retained"]>0
        assert r["Q1_new_half_true_3D_M_all_nonzero_count"]>r[
            "true_original_cut_Q1_physical_full_3D_nodes_all_modes"]
        assert r["Neumann_constant_rel_residual"]<1e-10
        assert r["exact_affine_x_plus_z_physical_Q1_gradient_stiffness_rel_error"]<2e-9
        assert r["native_impulse_full250ms_source_receiver_unmodified"]
        stair=r["original_real_native_staircase_oblique_roof_flux_audit"]
        assert stair["original_true_native_raw_boundary_node_count"]>0
        assert stair["original_staircase_roof_positive_y_axis_missing_face_count"]>0
        assert stair["original_staircase_roof_positive_z_axis_missing_face_count"]>0
        assert .45<stair[
            "actual_original_stair_boundary_axis_flux_affine_u_y_minus_quarter_z_rms"]<.55
        assert abs(stair["physical_sloped_roof_exact_affine_u_y_minus_quarter_z_normal_flux"])<1e-13
        assert stair["not_a_canonical_fullwave_nonconvergence_causality_proof"]
        np.testing.assert_allclose(
            unpairs(r["preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80"]),
            unpairs(con[k]["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"]))
        np.testing.assert_allclose(
            unpairs(r["preobserved_full_positive_lump_Q1_original_signed_40_80"]),
            unpairs(lump[k]["new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"]))
        np.testing.assert_allclose(
            unpairs(r["original_full250ms_native_real_PFFDTD_q0_signed_40_80"]),
            unpairs(real[k]["unmodified_original_transfer_pa_per_m3_s"]))
        assert np.isfinite(unpairs(
            r["new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80"])).all()
        roofnew=r["new_original_q0_Q1_lumped_first_roof_echo_causal_weak_all_widths"]
        assert [a["physical_width_s"] for a in roofnew]==list(ROOF_WIDTHS_S)
        for j,q in enumerate(roofnew):
            assert q["NEW_half_true_roof_original_native_allmode_window"][
                "all_original_native_3D_modes_retained"]==r[
                    "true_original_cut_Q1_physical_full_3D_nodes_all_modes"]
            assert q["preobserved_original_true_PFFDTD_native_roof_weak"][
                "native_window_to_true_roof_image_signed_ratio"]==origroof[k][
                    "new_original_q0_roof_echo_fixed_weak_width_cases"][j][
                    "native_total_roof_window_vs_single_roof_analytic_signed_ratio"]
            assert q["NEW_half_true_roof_original_native_allmode_window"][
                "analytical_causal_Newmark_modal_pressure_not_fitted"]
    theory=evidence["parameter_free_1D_dispersion_theory"]
    assert theory["fixed_blend_coefficient_without_fitting"]==.5
    kh=np.asarray(theory["kh"])
    for kind,key in [("half","half_M_lambda_over_true_wave_k2"),
        ("consistent","consistent_M_lambda_over_true_wave_k2"),
        ("lumped","lumped_M_lambda_over_true_wave_k2")]:
        np.testing.assert_allclose(theory[key],wave_uniform_1d_symbol_ratio(
            kh,mass_kind=kind),atol=1e-15)
    items=evidence[
        "complete_original_signed_4adjacent_3gates_vs_all_preobserved_cut_Q1_and_original_control"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in items]==[
        (28,32),(32,36),(36,40),(40,44)]
    arms={
        "new_theory_fixed_half_consistent_half_lump_Q1":"new_true_roof_Q1_half_theory_mass_allmode_full_250ms_original_signed_40_80",
        "preobserved_positive_row_sum_lumped_cut_Q1":"preobserved_full_positive_lump_Q1_original_signed_40_80",
        "preobserved_consistent_mass_cut_Q1":"preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80",
        "original_unmodified_PFFDTD_q0":"original_full250ms_native_real_PFFDTD_q0_signed_40_80"}
    for i,rec in enumerate(items):
        assert set(rec["arms"])==set(arms)
        for arm,key in arms.items():
            observed=compare_complex_transfer(reference=rows[i+1][key],
                candidate=rows[i][key],frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            q=rec["arms"][arm]
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(observed[metric]-q[
                    "unchanged_original_full_250ms_40_80_signed_scores"][metric])<1e-10
            assert not q["all_original_three_frozen_gates_PASS"]
    for v in evidence["complete_full5grid_native_q0_still_unqualified_verdict"].values():
        assert not v["all_4adjacent_original_three_frozen_gates_pass"]
        assert not v["strict_original_three_metric_monotone"]
        assert v["no_canonical_upstream_qualification"]
    worst=items[-1]["arms"]["new_theory_fixed_half_consistent_half_lump_Q1"]
    assert worst["unchanged_original_full_250ms_40_80_signed_scores"][
        "complex_rms_relative"]>1.2
