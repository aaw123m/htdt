"""Independent actual original SHA q0 all-five physical cut Q1 row-lump tests.

No fitted source, no dropped roof sliver nodes/high modes, no alteration
to original full 250ms signed 40/80 or the previously frozen causal roof
echo witnesses. This experimentally changes physical mass only.
"""
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
from htdt.r130d_conforming_roof_p1_fem import (
    build_original_native_conforming_roof_p1)
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators)
from htdt.r130d_native_cut_Q1_positive_lumped import (
    physical_positive_row_sum_lump,true_full_3d_lumped_roof_MK,
    allmode_native_newmark_roof_causal_pressure_weak)
from htdt.r130d_causal_first_roof_echo import ROOF_WIDTHS_S,ROOF_CENTER_S
from htdt.r130d_causal_prefirst_weak import compact_odd_witness
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_q0_exact_roof_Q1_lumped_mass import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_evidence_2026-10-09.json"
Q1_REFERENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"
ORIGINAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
ROOF=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json"


def small_physical_Q1():
    axes=np.arange(-.2,4.201,.2)
    f=build_original_native_conforming_roof_p1(
        [axes,axes,axes],max_yz_nodes=1000,max_3d_nodes=40000)
    mx,my,kx,ky=consistent_physical_P1_tensored_operators(f)
    q=cut_roof_native_original_Q1_galerkin_yz(
        axes,axes,max_active_yz_nodes=1100)
    return f,q,mx,my,kx,ky


def test_true_physical_cut_Q1_positive_row_sum_mass_exact_56m3_and_no_floor():
    f,q,mx,my,kx,ky=small_physical_Q1()
    l=physical_positive_row_sum_lump(mx,q.mass,kx,q.stiffness)
    assert q.occupied_cartesian_original_exterior_nodes>0
    assert q.minimum_strict_positive_cut_polygon_m2>0
    assert l.Mx.nnz==len(f.x_positions_m)
    assert l.Myz.nnz==q.native_yz_modes
    np.testing.assert_allclose(l.physical_x_rowsum_m,mx@np.ones(mx.shape[0]),atol=1e-14)
    np.testing.assert_allclose(l.physical_yz_rowsum_m2,q.mass@np.ones(q.native_yz_modes),atol=1e-14)
    assert l.physical_yz_rowsum_m2.min()>0
    assert l.physical_x_rowsum_m.min()>0
    assert abs(l.physical_x_rowsum_m.sum()-4)<1e-9
    assert abs(l.physical_yz_rowsum_m2.sum()-14)<2e-8
    M,K=true_full_3d_lumped_roof_MK(l)
    assert M.shape[0]==len(f.x_positions_m)*q.native_yz_modes
    assert M.nnz==M.shape[0]
    assert np.min(M.diagonal())>0
    assert abs(float(M.sum())-56)<2e-8
    const=np.ones(M.shape[0])
    assert max(abs(K@const))/max(1,max(abs(K.diagonal())))<1e-10
    x=f.x_positions_m[:,None]
    z=q.physical_active_yz_node_positions_m[None,:,1]
    affine=(x+z).ravel()
    assert abs(float(affine@(K@affine))/(112*343.2**2)-1)<2e-9
    # Row lump does NOT preserve the exact affine squared consistent mass.
    # Do not quietly present affine kinetic moment as exact.
    assert abs(float(affine@(M@affine))-2780/3)>1e-3


def test_positive_roof_lump_rejects_zero_or_negative_real_node_mass():
    f,q,mx,my,kx,ky=small_physical_Q1()
    with pytest.raises(ValueError,match="positive physical row-sum"):
        physical_positive_row_sum_lump(mx,-q.mass,kx,q.stiffness)
    with pytest.raises(ValueError,match="invalid original true"):
        physical_positive_row_sum_lump(mx[:3,:3],q.mass,kx,q.stiffness)


@pytest.mark.parametrize("width",ROOF_WIDTHS_S)
def test_allmode_original_Newmark_native_roof_interior_pressure_matches_explicit_trace(width):
    dt=.00017
    nt=1471
    eigenvalues=np.array([0.,(.9*2*np.pi*40)**2,
        (2*np.pi*600)**2,(2*np.pi*9000)**2],dtype=float)
    amp=np.array([.07,-.2,.11,.13])
    theta=2*np.arctan(.5*dt*np.sqrt(eigenvalues))
    # Causal original beta=1/4 Newmark, sample n potential signal.
    n=np.arange(nt,dtype=float)
    phi=np.zeros(nt)
    for a,t in zip(amp,theta):
        if t==0: phi+=a*n
        else: phi+=a*np.sin(n*t)/np.sin(t)
    pressure=np.empty(nt)
    pressure[0]=1.2*(-3*phi[0]+4*phi[1]-phi[2])/(2*dt)
    pressure[1:-1]=1.2*(phi[2:]-phi[:-2])/(2*dt)
    pressure[-1]=1.2*(3*phi[-1]-4*phi[-2]+phi[-3])/(2*dt)
    w=compact_odd_witness(n*dt,width,center_s=ROOF_CENTER_S,
        radius_s=.0011)
    full_native=float(pressure@w)
    exact=allmode_native_newmark_roof_causal_pressure_weak(
        eigenvalues,amp,dt,nt)
    found=next(r for r in exact if r["width_s"]==width)
    assert found["all_original_native_3D_modes_retained"]==4
    assert abs(full_native-found[
        "original_native_q0_same_record_allmode_weak_p_over_Q"])<1e-8


@pytest.mark.parametrize("parameter",[
    "complex_gate","magnitude_gate","phase_deg_gate"])
def test_precommitted_original_frozen_gates_not_relaxed(parameter):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["frozen_original"][parameter]=90
    with pytest.raises(ValueError,match="prospectively frozen Q1"):
        validate_plan(p)


def test_all_original_five_grid_real_HDF5_exact_roof_Q1_lump_failed_and_full_modes_retained():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    evidence=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    q1=json.loads(Q1_REFERENCE.read_text(encoding="utf-8"))
    orig=json.loads(ORIGINAL.read_text(encoding="utf-8"))
    roof=json.loads(ROOF.read_text(encoding="utf-8"))
    assert evidence["precommitted_plan"]==p
    assert evidence["frozen_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert evidence["unchanged_original_PFFDTD_self_convergence"]=="SELF_CONVERGENCE_FAILED"
    assert evidence["independent_physical_validation"]=="NOT_VALIDATED"
    assert evidence["product"]=="NO_GO"
    assert evidence["new_original_native_PFFDTD_runs"]==evidence["new_GitHub_Actions_runs"]==0
    assert evidence["no_production_go_or_independent_physical_validation"]
    rows=evidence["actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]
    prev={v["ppw"]:v for v in q1["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    real={v["ppw"]:v for v in orig["actual_native_wave_cases"]}
    oldroof={v["ppw"]:v for v in roof["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    assert [v["ppw"] for v in rows]==list(PPW)
    assert sum(v["true_original_cut_Q1_physical_full_3D_nodes_all_modes"] for v in rows)==401630
    for v in rows:
        k=v["ppw"]
        assert v["original_SHA256_comms"]==real[k]["original_native_comm_sha256"]
        assert v["original_SHA256_voxel"]==real[k]["original_solver_geometry_sha256"]
        assert v["original_SHA256_full_native_real_wave"]==oldroof[k][
            "original_true_native_entire_real_q0_HDF5_SHA256"]
        assert abs(v["native_original_Ts_s"]-prev[k]["original_native_Ts_s"])<1e-12
        assert v["native_original_Nt"]==prev[k]["original_native_full_Nt"]
        assert v["true_original_cut_Q1_physical_full_3D_nodes_all_modes"]==prev[k][
            "real_original_native_3D_full_true_Q1_modes"]
        assert abs(v["physical_exact_3D_volume_m3"]-56)<2e-8
        assert v["positive_yz_cut_Q1_lump_min_mass_m2"]>0
        assert v["original_true_positive_outside_support_ghost_nodes_retained"]>0
        assert v["Q1_new_true_lumped_3D_M_diagonal_nnz"]==v[
            "true_original_cut_Q1_physical_full_3D_nodes_all_modes"]
        assert v["Neumann_constant_rel_residual"]<1e-10
        assert v["exact_affine_x_plus_z_physical_Q1_gradient_stiffness_rel_error"]<2e-9
        assert v["native_impulse_full250ms_source_receiver_unmodified"]
        for proof in ("x_all_modes_generalized_eigencheck","yz_all_modes_generalized_eigencheck"):
            x=v[proof]
            assert x["strict_max_M_orthogonality_residual"]<5e-8 if proof.startswith("x_") else x[
                "strict_M_orthonormality_Gram_maxerror"]<5e-8
        np.testing.assert_allclose(
            unpairs(v["preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80"]),
            unpairs(prev[k]["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"]))
        np.testing.assert_allclose(
            unpairs(v["original_full250ms_native_real_PFFDTD_q0_signed_40_80"]),
            unpairs(real[k]["unmodified_original_transfer_pa_per_m3_s"]))
        assert np.isfinite(unpairs(v[
            "new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"])).all()
        windows=v["new_original_q0_Q1_lumped_first_roof_echo_causal_weak_all_widths"]
        assert [x["physical_width_s"] for x in windows]==list(ROOF_WIDTHS_S)
        for j,item in enumerate(windows):
            q=item["NEW_lumped_true_roof_original_native_allmode_window"]
            ref=item["unchanged_true_64node_physical_roof_image_analytic"]
            assert q["all_original_native_3D_modes_retained"]==v[
                "true_original_cut_Q1_physical_full_3D_nodes_all_modes"]
            assert q["analytical_causal_Newmark_modal_pressure_not_fitted"]
            assert ref["earliest_actual_original_nonroof_single_bounce_s"]>ROOF_CENTER_S+.0011
            predicted=ref["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
            value=q["original_native_q0_same_record_allmode_weak_p_over_Q"]
            assert abs(value/predicted-item[
                "new_native_Q1_lumped_weak_roof_vs_original_roof_image_signed_ratio"])<1e-10
            assert abs(item[
                "preobserved_original_true_PFFDTD_native_roof_weak"][
                    "native_window_to_true_roof_image_signed_ratio"]-
                  oldroof[k]["new_original_q0_roof_echo_fixed_weak_width_cases"][j][
                    "native_total_roof_window_vs_single_roof_analytic_signed_ratio"])<1e-10
    pairs=evidence["complete_original_signed_4adjacent_3gates_vs_true_cut_Q1_consistent_control"]
    assert [(r["coarse_ppw"],r["fine_ppw"]) for r in pairs]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,row in enumerate(pairs):
        assert set(row["arms"])=={
           "new_positive_exact_mass_lumped_cut_Q1",
           "preobserved_consistent_mass_cut_Q1",
           "original_unmodified_PFFDTD_q0"}
        for arm,key in [
          ("new_positive_exact_mass_lumped_cut_Q1",
           "new_true_roof_Q1_positive_lumped_mass_allmode_full_250ms_original_signed_40_80"),
          ("preobserved_consistent_mass_cut_Q1",
           "preobserved_consistent_Q1_true_roof_allmode_full_250ms_signed_40_80"),
          ("original_unmodified_PFFDTD_q0",
           "original_full250ms_native_real_PFFDTD_q0_signed_40_80")
        ]:
            actual=compare_complex_transfer(reference=rows[i+1][key],
                candidate=rows[i][key],frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            got=row["arms"][arm]
            for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(actual[k]-got["unchanged_original_full_250ms_40_80_signed_scores"][k])<1e-10
            passed=actual["complex_rms_relative"]<=.2 and actual["magnitude_max_relative"]<=.25 and actual["phase_max_deg"]<=15
            assert passed is got["all_original_three_frozen_gates_PASS"]
            assert not passed
    report=evidence["complete_full5grid_native_q0_still_unqualified_verdict"]
    for r in report.values():
        assert not r["all_4adjacent_original_three_frozen_gates_pass"]
        assert not r["strict_original_three_metric_monotone"]
        assert r["no_canonical_upstream_qualification"]
    last=pairs[-1]["arms"]["new_positive_exact_mass_lumped_cut_Q1"]
    s=last["unchanged_original_full_250ms_40_80_signed_scores"]
    assert s["complex_rms_relative"]<.2 and s["phase_max_deg"]<15
    assert s["magnitude_max_relative"]>.25
