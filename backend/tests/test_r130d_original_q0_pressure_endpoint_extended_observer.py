"""Independent pressure finite-difference replay and all original q0 A/B FAIL."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_native_modal_drift import unpairs
from run_r130d_original_pffdtd_native_full_modal_q0 import stable_sin_ratio
from run_r130d_original_q0_pressure_endpoint_extended_observer import (
    ARMS,extended_end_pressure_changes,validate_plan)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_pressure_endpoint_extended_observer_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_pressure_endpoint_extended_observer_evidence_2026-10-09.json"
PRIOR=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"
LAST=ROOT/"benchmarks/acoustics/r130d_original_q0_finite_window_endpoint_leakage_evidence_2026-10-09.json"


@pytest.mark.parametrize("dt,nt",[(.001,8),(.0002,23),(.00014,101)])
def test_independent_direct_wave_plus_one_homogeneous_step_exact_pressure(dt,nt):
    theta=np.array([0,.013,.18,.83,1.9,2.6])
    amplitude=np.array([.5,-1.8,.91,.07,-.02,2.3])
    rho=1.2
    # n=0..N appended ONE extra homogeneous state u[N], never scored itself.
    wave=np.stack([amplitude*stable_sin_ratio(i,theta) for i in range(nt+1)])
    old_final=rho*(3*wave[nt-1]-4*wave[nt-2]+wave[nt-3])/(2*dt)
    true_extended_centered=rho*(wave[nt]-wave[nt-2])/(2*dt)
    old_initial=rho*(-3*wave[0]+4*wave[1]-wave[2])/(2*dt)
    anti_causal_hypothetical_initial=rho*amplitude/dt
    changes=extended_end_pressure_changes(theta,amplitude,dt,nt,rho)
    for k,f in enumerate((40.,80.)):
        expected_end=np.exp(2j*np.pi*f*dt*(nt-1))*(true_extended_centered-old_final)
        np.testing.assert_allclose(changes[0,k],expected_end,rtol=5e-11,atol=2e-8)
        np.testing.assert_allclose(changes[1,k],
                                   anti_causal_hypothetical_initial-old_initial,
                                   rtol=5e-11,atol=2e-8)


def test_original_q0_pressure_operator_precommitted_no_relaxed_score():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert [a["id"] for a in p["arms"]]==list(ARMS)
    assert p["original"]["ppw"]==list(PPW)
    assert p["original"]["physical_source_xyz"]==[1.5,2,2]
    assert p["original"]["physical_receiver_xyz"]==[2.5,2,2]
    assert p["original"]["full_record_s"]==.25
    assert p["original"]["scored_freq_hz"]==[40,80]
    assert p["numerical"]["phi_N_extra_state_only_end_derivative"]
    assert p["limits"]["new_native_pffdtd_waves"]==0
    assert p["limits"]["new_github_actions_runs"]==0


@pytest.mark.parametrize("field,value",[
    ("original_complex_limit",1.0),
    ("original_magnitude_limit",.8),
    ("original_phase_deg_limit",170)])
def test_any_threshold_relaxation_fail_closed(field,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["evaluation"][field]=value
    with pytest.raises(ValueError,match="gates changed"):
        validate_plan(p)


def test_every_real_original_hdf5_native_full_mode_five_grid_pressure_A_B_negative():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    ev=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    earlier=json.loads(PRIOR.read_text(encoding="utf-8"))
    pre_window=json.loads(LAST.read_text(encoding="utf-8"))
    assert ev["preregistered_plan"]==p
    assert ev["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert ev["canonical_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert ev["physical_validation"]=="NOT_VALIDATED" and ev["product"]=="NO_GO"
    assert ev["new_PFFDTD_waves"]==ev["new_GitHub_Actions_runs"]==0
    assert ev["hypothetical_both_sided_centered_first_sample_is_not_causal"]
    old={z["ppw"]:z for z in earlier["actual_original_unmodified_all_mode_native_cases"]}
    parts={z["ppw"]:z for z in pre_window["actual_original_full_3D_all_modal_time_parts"]}
    cases=ev["original_real_five_grid_pressure_observer_cases"]
    assert [v["ppw"] for v in cases]==list(PPW)
    assert sum(c["original_native_3d_full_modes"] for c in cases)==347154
    for row in cases:
        ppw=row["ppw"]
        assert row["original_q0_and_all_mode_no_taper_no_cut"]
        assert row["one_true_homogeneous_post_record_extra_state_without_extra_scored_sample"]
        assert row["original_native_3d_full_modes"]==old[ppw]["actual_native_full_3D_modes_count"]
        assert row["native_record_nt"]==old[ppw]["original_record_samples"]
        assert row["native_dt_s"]==old[ppw]["original_native_time_step_s"]
        assert row["original_source_comms_sha256"]==old[ppw]["original_native_comms_SHA256"]
        assert row["original_room_voxel_sha256"]==old[ppw]["original_native_voxels_SHA256"]
        assert row["original_solver_unchanged_baseline_vs_saved_real_wave_relative"]<2e-6
        all_arms=row["original_record_full_250ms_signed_40_80_by_pressure_arm"]
        assert set(all_arms)==set(ARMS)
        initial=unpairs(all_arms["original_one_sided_end"])
        full=unpairs(parts[ppw]["original_native_full_250ms_signed_transfer_40_80"])
        assert np.linalg.norm(initial-full)/max(np.linalg.norm(full),1e-12)<2e-10
        end=unpairs(all_arms["extended_centered_end"])
        both=unpairs(all_arms["hypothetical_all_centered"])
        assert np.allclose(end-initial,
            unpairs(row["difference_extended_end_minus_original_signed_40_80"]),atol=1e-8)
        assert np.allclose(both-end,
            unpairs(row["difference_hypothetical_start_center_minus_original_signed_40_80"]),atol=1e-8)
        assert np.all(np.isfinite(initial)) and np.all(np.isfinite(end))
    scores=ev["all_four_original_adjacent_pressure_observer_comparisons"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,ab in enumerate(scores):
        assert set(ab["arms"])==set(ARMS)
        for arm in ARMS:
            c=cases[i]["original_record_full_250ms_signed_40_80_by_pressure_arm"][arm]
            f=cases[i+1]["original_record_full_250ms_signed_40_80_by_pressure_arm"][arm]
            row=ab["arms"][arm]
            assert np.allclose(unpairs(row["signed_coarse_minus_fine_40_80"]),
                               unpairs(c)-unpairs(f),rtol=1e-11,atol=1e-8)
            actual=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            for name in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(actual[name]-row["all_frozen_original_complex_magnitude_phase"][name])<1e-11
            expected=(actual["complex_rms_relative"]<=.2 and
                      actual["magnitude_max_relative"]<=.25 and
                      actual["phase_max_deg"]<=15)
            assert expected is row["passes_original_three_gates"]
        # The newer last-stencil operator is NOT original, never conflate.
        assert not ab["arms"]["extended_centered_end"]["passes_original_three_gates"]
    for arm in ARMS:
        x=ev["original_and_diagnostic_pressure_observer_verdicts"][arm]
        assert not x["all_four_pairs_pass_three_gates"]
        assert not x["three_error_types_monotone_decrease"]
        assert not x["valid_for_original_canonical_requalification"]
