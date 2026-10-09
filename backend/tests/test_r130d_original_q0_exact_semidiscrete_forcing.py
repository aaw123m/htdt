"""Causal semidiscrete q0 oscillator exact-solver independent direct-time proof."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_exact_semidiscrete_q0 import (
    exact_semidiscrete_velocity_impulse_signed,
    exact_semidiscrete_one_sample_hold_signed,
    exact_semidiscrete_one_sample_hold_phi)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_native_modal_drift import unpairs
from run_r130d_original_q0_exact_semidiscrete_forcing import (
    ARMS,validate_plan)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_semidiscrete_forcing_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_semidiscrete_forcing_evidence_2026-10-09.json"
NATIVE=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"


@pytest.mark.parametrize("nt,dt",[(9,.001),(20,.0002),(124,.00014)])
def test_exact_held_q0_and_delta_impulse_vs_independent_direct_harmonic_wave(nt,dt):
    # Explicitly integrate the exact constant-force oscillator for [0,dt]:
    # u(dt)=a(1-cos(omega*dt))/omega², v(dt)=a sin(omega*dt)/omega.
    # Then exact homogeneous dynamics, and direct pressure stencils + DFT.
    theta=np.array([0.,.018,.11,.29,1.2,2.7],dtype=float)
    amplitude=np.array([1.2,-.8,2.,.03,-1.6,.77],dtype=float)
    th2=theta.copy();th2[0]=1
    rho=1.2
    u1=amplitude*(1-np.cos(th2))/th2**2
    v1_dt=amplitude*np.sin(th2)/th2
    u1[0]=amplitude[0]/2
    v1_dt[0]=amplitude[0]
    hold=np.zeros((nt,theta.size))
    impulse=np.zeros((nt,theta.size))
    for n in range(1,nt):
        hold[n]=u1*np.cos((n-1)*theta)+(
            v1_dt*(n-1)*np.sinc((n-1)*theta/np.pi))
        impulse[n]=amplitude*n*np.sinc(n*theta/np.pi)
        np.testing.assert_allclose(
            exact_semidiscrete_one_sample_hold_phi(n,theta,amplitude),
            hold[n],rtol=5e-11,atol=3e-11)
    for i,(wave,func) in enumerate((
        (impulse,exact_semidiscrete_velocity_impulse_signed),
        (hold,exact_semidiscrete_one_sample_hold_signed))):
        pressure=np.empty_like(wave)
        pressure[0]=rho*(-3*wave[0]+4*wave[1]-wave[2])/(2*dt)
        pressure[1:-1]=rho*(wave[2:]-wave[:-2])/(2*dt)
        pressure[-1]=rho*(3*wave[-1]-4*wave[-2]+wave[-3])/(2*dt)
        predicted=func(theta,amplitude,native_dt_s=dt,native_nt=nt,density_kg_m3=rho)
        for j,f in enumerate((40.,80.)):
            true=(pressure*np.exp(2j*np.pi*f*dt*np.arange(nt)[:,None])).sum(axis=0)
            np.testing.assert_allclose(predicted[j],true,rtol=2e-9,atol=4e-7)


@pytest.mark.parametrize("bad",["original_frozen_complex","original_frozen_magnitude",
                                 "original_frozen_phase_deg"])
def test_original_frozen_three_gate_scores_fail_closed(bad):
    plan=json.loads(PLAN.read_text(encoding="utf-8"))
    plan["tests"][bad]=1e3
    with pytest.raises(ValueError,match="preregistered plan drift"):
        validate_plan(plan)


def test_exact_q0_numeric_plan_original_wave_source_cannot_move():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert [x["id"] for x in p["temporal_arms"]]==list(ARMS)
    assert p["original"]["ppw"]==list(PPW)
    assert p["original"]["physical_source_m"]==[1.5,2,2]
    assert p["original"]["physical_receiver_m"]==[2.5,2,2]
    assert p["original"]["record_s"]==.25
    assert p["original"]["signed_frequency_hz"]==[40,80]
    assert p["limits"]["original_native_pffdtd_new_waves"]==0
    assert p["limits"]["new_github_actions_runs"]==0


def test_full_true_original_q0_five_grids_all_three_time_schemes_and_no_disguised_pass():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    old=json.loads(NATIVE.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_native_pffdtd_waves"]==e["new_GitHub_Actions_runs"]==0
    assert e["new_numerical_time_solvers_not_original_pffdtd_qualification"]
    cases=e["actual_original_native_q0_five_grid_full_modal_cases"]
    prior={x["ppw"]:x for x in old["actual_original_unmodified_all_mode_native_cases"]}
    assert [q["ppw"] for q in cases]==list(PPW)
    assert sum(q["native_original_full_graph_3d_modes_count"] for q in cases)==347154
    for q in cases:
        ppw=q["ppw"]
        assert q["all_original_source_receiver_eigenmodes_untruncated"]
        assert q["new_time_integrators_not_original_PFFDTD"]
        assert q["native_original_full_graph_3d_modes_count"]==prior[ppw]["actual_native_full_3D_modes_count"]
        assert q["original_native_dt_s"]==prior[ppw]["original_native_time_step_s"]
        assert q["original_Nt"]==prior[ppw]["original_record_samples"]
        assert q["original_source_comm_SHA256"]==prior[ppw]["original_native_comms_SHA256"]
        assert q["original_room_voxel_SHA256"]==prior[ppw]["original_native_voxels_SHA256"]
        assert q["original_control_vs_true_raw_saved_pressure_rel"]<2e-6
        assert q["original_control_vs_prior_untruncated_full_modes_rel"]<2e-10
        assert set(q["signed_original_full_250ms_P_over_Q_40_80_by_arm"])==set(ARMS)
        for arm in ARMS:
            assert np.isfinite(unpairs(q["signed_original_full_250ms_P_over_Q_40_80_by_arm"][arm])).all()
        assert np.allclose(unpairs(q["signed_original_full_250ms_P_over_Q_40_80_by_arm"]
                                    ["original_native_leapfrog"]),
                           unpairs(prior[ppw]["exact_original_saved_true_PFFDTD_q0_signed_40_80"]),
                           atol=5e-7)
    score=e["all_four_adjacent_native_q0_refinements"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in score]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,row in enumerate(score):
        assert set(row["arms"])==set(ARMS)
        for arm in ARMS:
            c=cases[i]["signed_original_full_250ms_P_over_Q_40_80_by_arm"][arm]
            f=cases[i+1]["signed_original_full_250ms_P_over_Q_40_80_by_arm"][arm]
            sub=row["arms"][arm]
            assert np.allclose(
                unpairs(sub["two_original_frequency_signed_coarse_minus_fine"]),
                unpairs(c)-unpairs(f),rtol=1e-10,atol=1e-8)
            recompute=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            for name in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(sub["full_original_three_metric_scoring"][name]-recompute[name])<1e-10
            flag=(recompute["complex_rms_relative"]<=.2 and
                  recompute["magnitude_max_relative"]<=.25 and
                  recompute["phase_max_deg"]<=15)
            assert flag is sub["three_frozen_limits_simultaneously_passed"]
    assert set(e["all_arms_full_five_grid_refinement_verdict"])==set(ARMS)
    for arm,v in e["all_arms_full_five_grid_refinement_verdict"].items():
        assert v["canonical_original_PFFDTD_status_unchanged"]
        assert not (v["all_four_adjacent_pairs_three_gates_pass"] and
                    v["all_three_frozen_metrics_strictly_monotone_decrease"])
