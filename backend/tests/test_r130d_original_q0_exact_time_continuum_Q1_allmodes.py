"""Independent exact-time Neumann allmode original q0 wave & adverse 5grid.

Prospective plan already pushed before 15 original HDF5 PPW/mass scores.
Use original *entire* sampled 250ms P_T/Q_T and existing fixed first
physical roof impulse witnesses, never short-time gate substitution.
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
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from htdt.r130d_exact_semidiscrete_causal_q0 import (
    _geometric_positive_frequency_progression,
    exact_continuous_modal_delta_impulse_signed_original_250ms,
    exact_continuous_modal_delta_impulse_roof_weak)
from htdt.r130d_causal_prefirst_weak import compact_odd_witness
from htdt.r130d_causal_first_roof_echo import (
    ROOF_WIDTHS_S,ROOF_CENTER_S,ROOF_RADIUS_S)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_exact_time_continuum_Q1_allmodes import (
    ARMS,SOURCE_KEYS,validate_plan)
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_time_continuum_impulse_cut_Q1_allmodes_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_time_continuum_cut_Q1_allmodes_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
CONSISTENT=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"
LUMP=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_evidence_2026-10-09.json"
HALF=ROOT/"benchmarks/acoustics/r130d_original_q0_oblique_roof_theory_half_Q1_evidence_2026-10-09.json"
ROOF=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json"


@pytest.mark.parametrize("beta",[
    np.array([0.,2*np.pi,-2*np.pi,4*np.pi,2*np.pi+1e-12]),
    np.array([.02,-.1,.4,6*np.pi+.02,-8*np.pi-.02]),
    np.array([1e-12,-1e-12,5e-6,-.02,.22])])
def test_finite_original_q0_analytic_harmonic_sum_stable_at_all_sampling_aliases(beta):
    m=267
    calculated=_geometric_positive_frequency_progression(m,beta)
    reference=np.sum(np.exp(1j*np.arange(1,m+1)[:,None]*beta[None,:]),axis=0)
    np.testing.assert_allclose(calculated,reference,rtol=4e-10,atol=2e-9)
    assert np.isfinite(calculated).all()


@pytest.mark.parametrize("dt,nt",[(.000145,1719),(.00017,1471)])
def test_exact_time_allmode_250ms_signed_pressure_matches_independent_entire_time_samples(dt,nt):
    # Multiple frequencies above original native pressure Nyquist retained.
    omega=2*np.pi*np.array([0,40,80,400,1200,5500,11000],dtype=float)
    lam=omega**2
    cp=np.array([.006,-.032,.017,.05,-.011,.044,.015])
    analytic=exact_continuous_modal_delta_impulse_signed_original_250ms(
        lam,cp,dt,nt)
    n=np.arange(nt,dtype=float)
    theta=omega*dt
    a=(343.2*dt)**2*cp
    # Exact physical continuum impulse causal sin/omega, fully independent
    # time-domain trace; use original PFFDTD pressure finite difference.
    phi=np.zeros(nt)
    for j,t in enumerate(theta):
        phi+=a[j]*n*np.sinc(n*t/np.pi)
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=1.2)
    q=np.zeros(nt);q[0]=1
    directly=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    np.testing.assert_allclose(np.sum(analytic,axis=1),directly,
                               rtol=4e-9,atol=7e-7)
    assert analytic.shape==(2,len(lam))


@pytest.mark.parametrize("width",ROOF_WIDTHS_S)
def test_exact_time_native_first_roof_weak_matches_true_brute_original_pressure_samples(width):
    dt=.000145
    Nt=1725
    freqs=np.array([0,35,90,620,2000,8000],dtype=float)
    lam=(2*np.pi*freqs)**2
    cp=np.array([.002,.018,-.009,.018,.025,-.03])
    theta=dt*np.sqrt(lam)
    n=np.arange(Nt,dtype=float)
    A=(343.2*dt)**2*cp
    phi=np.zeros(Nt)
    for a,t in zip(A,theta):
        phi+=a*n*np.sinc(n*t/np.pi)
    p=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=1.2)
    witness=compact_odd_witness(
        n*dt,width,center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
    reference=float(np.dot(p,witness))
    outputs=exact_continuous_modal_delta_impulse_roof_weak(
        lam,cp,dt,Nt)
    r=next(x for x in outputs if x["physical_causal_roof_witness_width_s"]==width)
    assert r["all_physical_3d_generalized_modes_retained"]==len(lam)
    assert r["mathematically_exact_time_delta_q0_no_damping_fit_or_highmode_cut"]
    assert abs(r["true_original_native_exact_time_allmode_roof_window_weak"]-reference)<2e-6


def test_exact_continuous_physical_delta_modal_wave_energy_is_constant_not_fitted():
    # For each mode, phi(t)=c²dt*s_e*sin(omega*t)/omega exactly,
    # E=0.5(phi_dot²+omega² phi²) constant, no Newmark artificial
    # temporal phase dispersion or high-mode damping required.
    dt=.00011
    omega=2*np.pi*np.array([40,850,8900])
    cp=np.array([.05,.02,-.03])
    t=np.array([0,.0001,.0033,.1,.24])
    for om,source in zip(omega,cp):
        B=343.2**2*dt*source
        phi=B*np.sin(om*t)/om
        derivative=B*np.cos(om*t)
        e=.5*(derivative**2+(om*phi)**2)
        np.testing.assert_allclose(e,np.full_like(e,.5*B*B),rtol=5e-14)


@pytest.mark.parametrize("k,val",[
    ("original_full_complex_rms_gate",.8),
    ("original_full_magnitude_max_gate",.9),
    ("original_full_phase_max_deg",45),
    ("original_full_wave_record_s",.1)])
def test_no_retrospective_relaxation_of_original_q0_full_record_or_gates(k,val):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["original_unchanged"][k]=val
    with pytest.raises(ValueError,match="original q0 exact-continuum-time"):
        validate_plan(p)


def test_real_five_ppw_original_full_250ms_exact_time_3mass_15complete_unfavorable():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    raw=json.loads(RAW.read_text(encoding="utf-8"))
    con=json.loads(CONSISTENT.read_text(encoding="utf-8"))
    lump=json.loads(LUMP.read_text(encoding="utf-8"))
    half=json.loads(HALF.read_text(encoding="utf-8"))
    roof=json.loads(ROOF.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_SHA256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["canonical_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["no_new_native_PFFDTD_reruns_or_manual_Actions"]
    assert e["all_high_modes_including_above_original_native_Nyquist_accounted_for"]
    assert e["release_original_upstream_r130d_still_FAILED_and_NO_GO"]
    rows=e["true_new_exact_time_on_actual_original_SHA_HDF5_five_PPWs"]
    assert [r["ppw"] for r in rows]==list(PPW)
    observed={
        "fully_consistent_Q1":{v["ppw"]:v for v in con[
            "actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]},
        "positive_row_sum_Q1":{v["ppw"]:v for v in lump[
            "actual_all_five_original_native_q0_positive_cut_Q1_lump_cases"]},
        "theory_half_Q1":{v["ppw"]:v for v in half[
            "actual_all_five_original_native_q0_theory_half_cut_Q1_cases"]}}
    original={v["ppw"]:v for v in raw["actual_native_wave_cases"]}
    oldroof={v["ppw"]:v for v in roof[
        "actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]}
    for row in rows:
        k=row["ppw"]
        assert row["original_unmodified_PFFDTD_real_comms_SHA256"]==original[k][
            "original_native_comm_sha256"]
        assert row["original_unmodified_PFFDTD_real_voxel_SHA256"]==original[k][
            "original_solver_geometry_sha256"]
        assert row["original_unmodified_PFFDTD_real_wave_SHA256"]==oldroof[k][
            "original_true_native_entire_real_q0_HDF5_SHA256"]
        assert row["native_original_dt_s"]==observed["fully_consistent_Q1"][k][
            "original_native_Ts_s"]
        assert row["native_original_Nt"]==observed["fully_consistent_Q1"][k][
            "original_native_full_Nt"]
        assert row["no_original_PFFDTD_sample_or_acceptance_changed"]
        assert abs(row["physical_true_14m2_original_roof_area"]-14)<2e-8
        assert row["physical_roof_original_outside_native_wet_support_nodes_kept"]>0
        np.testing.assert_allclose(
            unpairs(row["original_upstream_entire_real_native_wave_250ms_signed_40_80"]),
            unpairs(original[k]["unmodified_original_transfer_pa_per_m3_s"]))
        assert set(row["all_predeclared_exact_time_mass_arms"])==set(ARMS)
        for mass in ARMS:
            r=row["all_predeclared_exact_time_mass_arms"][mass]
            assert r["experimental_mass_family"]==mass
            assert r["all_original_physical_Q1_cut_3D_modes_retained"]==observed[mass][k][
                "true_original_cut_Q1_physical_full_3D_nodes_all_modes" if mass!="fully_consistent_Q1" else
                "real_original_native_3D_full_true_Q1_modes"]
            assert abs(r["total_physical_mass_3D_m3"]-56)<2e-8
            assert r["all_true_positive_roof_slivers_kept"]
            assert r["x_M_smallest_diagonal"]>0
            assert r["yz_M_smallest_diagonal"]>0
            assert r["original_spatial_neumann_operator_not_fitted"]
            assert r["all_original_true_physical_3D_modes_sampled_without_spectral_cut"]
            assert r["exact_time_wave_returned_is_EXPERIMENT_not_original_upstream"]
            assert np.isfinite(unpairs(r[
              "new_exact_time_original_delta_native_sampled_original_250ms_signed_40_80"])).all()
            np.testing.assert_allclose(
                unpairs(r["old_preobserved_original_beta_quarter_Newmark_Q1_250ms_signed_40_80"]),
                unpairs(observed[mass][k][SOURCE_KEYS[mass]]))
            weak=r["new_exact_time_first_original_roof_reflection_3_declared_physical_weak_widths"]
            assert [z["preregistered_witness_width_s"] for z in weak]==list(ROOF_WIDTHS_S)
            for z in weak:
                assert z["all_modes_exact_time_new_native_wave"][
                    "all_physical_3d_generalized_modes_retained"]==r[
                        "all_original_physical_Q1_cut_3D_modes_retained"]
                assert z["this_is_NOT_pure_reflection_coefficient_or_original_full250ms_acceptance"]
                actual=z["all_modes_exact_time_new_native_wave"][
                    "true_original_native_exact_time_allmode_roof_window_weak"]
                ref=z["frozen_exact_true_64native_node_finite_roof_image"][
                    "original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
                assert abs(actual/ref-z[
                    "exact_time_new_solver_total_roof_window_over_single_image_signed_ratio"])<1e-10
                assert abs(abs(actual/ref-1)-z[
                    "exact_time_new_solver_total_roof_window_vs_single_image_relative"])<1e-10
                assert z["frozen_exact_true_64native_node_finite_roof_image"][
                    "earliest_actual_original_nonroof_single_bounce_s"]>ROOF_CENTER_S+ROOF_RADIUS_S
    assert sum(row["all_predeclared_exact_time_mass_arms"][ARMS[0]][
        "all_original_physical_Q1_cut_3D_modes_retained"] for row in rows)==401630
    comparisons=e["original_250ms_full_four_adjacent_three_gates_all_four_arms"]
    assert [(r["coarse_ppw"],r["fine_ppw"]) for r in comparisons]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,x in enumerate(comparisons):
        assert len(x["control_and_exact_time"])==7
        for name,series in x["control_and_exact_time"].items():
            if name=="original_upstream_true_PFFDTD":
                coarse=rows[i]["original_upstream_entire_real_native_wave_250ms_signed_40_80"]
                fine=rows[i+1]["original_upstream_entire_real_native_wave_250ms_signed_40_80"]
            else:
                arm,timing=name.split("/")
                key=("new_exact_time_original_delta_native_sampled_original_250ms_signed_40_80"
                     if timing=="exact_continuum_time" else
                     "old_preobserved_original_beta_quarter_Newmark_Q1_250ms_signed_40_80")
                coarse=rows[i]["all_predeclared_exact_time_mass_arms"][arm][key]
                fine=rows[i+1]["all_predeclared_exact_time_mass_arms"][arm][key]
            score=compare_complex_transfer(reference=fine,candidate=coarse,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(score[metric]-series[
                    "original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics"][metric])<1e-10
            pass_all=(score["complex_rms_relative"]<=.2 and
                      score["magnitude_max_relative"]<=.25 and
                      score["phase_max_deg"]<=15)
            assert pass_all is series["original_all_three_gates_PASS"]
            assert not pass_all
    verdict=e["all_cases_verdict_do_not_promote_cherrypicked_gate"]
    assert len(verdict)==7
    for key,val in verdict.items():
        assert not val["all_four_pairs_pass_every_original_three_gate"]
        assert not val["strict_three_metric_monotone_convergence"]
        assert val["original_upstream_and_independent_BRAS_MFEM_NOT_qualified"]
    last=comparisons[-1]["control_and_exact_time"][
        "positive_row_sum_Q1/exact_continuum_time"]
    s=last["original_signed_40_80_full_unfiltered_250ms_frozen_three_metrics"]
    assert s["complex_rms_relative"]<.2 and s["phase_max_deg"]<15
    assert s["magnitude_max_relative"]>1
