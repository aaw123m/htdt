"""True physical first sloping Neumann roof echo vs original q0 full native waves.

Report the true 64-node geometric roof-only image but never mistake the
native weak total pressure (possible earlier direct dispersive tail) for
an exact room Green reflection coefficient. Original 250ms scores FAIL.
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
from htdt.r130d_causal_first_roof_echo import (
    ROOF_CENTER_S,ROOF_RADIUS_S,ROOF_END_S,ROOF_WIDTHS_S,
    analytic_native_64point_physical_roof_echo_weak)
from htdt.r130d_causal_prefirst_weak import (
    compact_odd_witness,native_original_wave_weak_transfer)
from htdt.r130d_retarded_point_green import (
    C,RHO,ROOM_WALLS,true_wall_specular_reflection)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_q0_first_roof_echo_causal_weak import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json"
ORIGINAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
DIRECT=ROOT/"benchmarks/acoustics/r130d_original_q0_causal_prefirst_green_normalization_evidence_2026-10-09.json"


@pytest.mark.parametrize("sigma",ROOF_WIDTHS_S)
def test_true_neumann_roof_arrival_compact_bump_analytic_derivative(sigma):
    s=np.array([1.5,2.,2.])
    r=np.array([2.5,2.,2.])
    roof=next(w for w in ROOM_WALLS if w.name=="true_sloping_roof")
    path=true_wall_specular_reflection(s,r,roof)
    assert path["reflecting_point_on_true_finite_room_wall"]
    assert abs(path["travel_time_s"]-ROOF_CENTER_S)<1e-15
    w=compact_odd_witness(np.array([ROOF_CENTER_S]),sigma,
        center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S,derivative=True)
    assert abs(w[0]-1/sigma)<1e-10
    expected=-RHO/(4*np.pi*path["single_bounce_path_m"]*sigma)
    src=np.repeat(s[None,:],8,axis=0)
    rec=np.repeat(r[None,:],8,axis=0)
    weights=np.ones(8)/8
    computed=analytic_native_64point_physical_roof_echo_weak(
        src,weights,rec,weights,width_s=sigma)
    assert abs(computed["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]/expected-1)<1e-13
    assert abs(computed["true_physical_point_roof_single_bounce_signed_weak_analytic"]/expected-1)<1e-13
    assert computed["physical_reflection_sign"]==1
    assert computed["true_finite_roof_valid_original_native_64_pair_count"]==64
    assert computed["earliest_actual_original_nonroof_single_bounce_s"]>ROOF_END_S
    assert computed["all_64_native_direct_wave_analytic_contribution_within_roof_window"]==0
    assert computed["roof_echo_only_analytic_causality_NOT_native_pulse_decomposition"]


def test_original_first_roof_bump_support_excludes_direct_and_other_first_walls():
    s=np.array([1.5,2.,2.])
    r=np.array([2.5,2.,2.])
    assert 1/C <ROOF_CENTER_S-ROOF_RADIUS_S
    assert ROOF_CENTER_S+ROOF_RADIUS_S<.011655011655011656
    for width in ROOF_WIDTHS_S:
        t=np.array([0.,1/C,ROOF_CENTER_S-ROOF_RADIUS_S,
                    ROOF_CENTER_S,ROOF_CENTER_S+ROOF_RADIUS_S,.011655011655011656])
        w=compact_odd_witness(t,width,center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
        derivative=compact_odd_witness(t,width,derivative=True,
             center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
        assert np.array_equal(w[[0,1,2,4,5]],np.zeros(5))
        assert np.array_equal(derivative[[0,1,2,4,5]],np.zeros(5))
        assert abs(derivative[3]-1/width)<1e-10


def test_actual_original_full_q0_wave_weak_center_can_change_without_modifying_u():
    nt=1800
    dt=.00015
    ts=np.arange(nt)*dt
    # Synthetic receiver trace, NOT an injected modified original q0.
    waveform=np.sin(2*np.pi*90*ts)[None,:]*np.ones((8,1))
    before=waveform.copy()
    weights=np.ones(8)/8
    q=native_original_wave_weak_transfer(waveform,weights,dt_s=dt,
        width_s=ROOF_WIDTHS_S[1],center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
    assert q["physical_witness_center_s"]==ROOF_CENTER_S
    assert q["physical_witness_support_end_s"]==ROOF_END_S
    assert q["physical_witness_nonzero_samples"]>=8
    assert np.array_equal(before,waveform)
    assert np.isfinite(q["original_q0_native_weak_pressure_over_unit_input"])


@pytest.mark.parametrize("key,value",[
    ("physical_support_radius_s",.003),
    ("expected_support_upper_s",.02),
    ("nonroof_earliest_point_first_wall_reflection_s",.009)])
def test_fail_closed_frozen_true_roof_echo_physical_geometry(key,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["diagnostic"][key]=value
    with pytest.raises(ValueError,match="prospective fixed true physical roof echo"):
        validate_plan(p)


def test_full_original_q0_all_five_roof_and_direct_real_native_negative_evidence():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    real=json.loads(ORIGINAL.read_text(encoding="utf-8"))
    old=json.loads(DIRECT.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["precommitted_plan_SHA256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_canonical_fullroom"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_upstream_original_PFFDTD_waves"]==e["new_GitHub_Actions_runs"]==0
    assert e["genuine_single_echo_continuum_only_not_exact_native_direct_tail_decomposition"]
    assert e["do_not_promote_original_production_or_physical_claim"]
    assert not e["canonical_original_all_pairs_accepted"]
    raw={r["ppw"]:r for r in real["actual_native_wave_cases"]}
    prev={r["ppw"]:r for r in old["actual_new_weak_results_original_SHA_native_5grid"]}
    rows=e["actual_original_q0_five_grid_real_first_roof_echo_weak_cases"]
    assert [r["ppw"] for r in rows]==list(PPW)
    for row in rows:
        k=row["ppw"]
        assert row["original_true_native_comms_HDF5_SHA256"]==raw[k]["original_native_comm_sha256"]
        assert row["original_true_native_voxel_HDF5_SHA256"]==raw[k]["original_solver_geometry_sha256"]
        assert row["original_true_native_entire_real_q0_HDF5_SHA256"]==prev[k][
            "original_sha256_full_real_250ms_native_wave"]
        assert abs(row["original_native_Ts_s"]-prev[k]["original_native_Ts_s"])<1e-12
        assert row["original_native_Nt"]==prev[k]["original_native_Nt"]
        assert row["original_full_record_relative_to_frozen"]<2e-6
        assert row["full_250ms_native_wave_never_filtered_or_windowed_for_authority"]
        np.testing.assert_allclose(
            unpairs(row["original_full_record_250ms_signed_40_80_replayed"]),
            unpairs(raw[k]["unmodified_original_transfer_pa_per_m3_s"]),rtol=2e-6,atol=1e-5)
        assert row["old_observed_causal_DIRECT_weak_green_control_3_widths"]==prev[k][
            "three_predeclared_compact_weak_distribution_tests"]
        echoes=row["new_original_q0_roof_echo_fixed_weak_width_cases"]
        assert [r["physical_original_roof_echo_weak_width_s"] for r in echoes]==list(ROOF_WIDTHS_S)
        for datum in echoes:
            actual=datum["original_entire_wave_native_roof_window_weak"]
            ref=datum["continuum_true_physical_neumann_roof_64_original_nodes_analytic"]
            assert actual["physical_witness_center_s"]==ROOF_CENTER_S
            assert actual["physical_witness_support_end_s"]==ROOF_END_S
            assert ref["earliest_actual_original_nonroof_single_bounce_s"]>ROOF_END_S
            assert ref["earliest_actual_original_8node_roof_single_echo_s"]<ROOF_END_S
            assert ref["all_64_native_direct_wave_analytic_contribution_within_roof_window"]==0
            assert 0<ref["true_finite_roof_valid_original_native_64_pair_count"]<=64
            assert datum["native_window_direct_numeric_dispersive_tail_may_be_present"]
            assert datum["not_original_full_record_acceptance"]
            prediction=ref["original_64pair_finite_roof_single_bounce_signed_weak_analytic"]
            observed=actual["original_q0_native_weak_pressure_over_unit_input"]
            assert prediction!=0 and np.isfinite(observed)
            assert abs(datum["native_total_roof_window_vs_single_roof_analytic_signed_ratio"]-
                       observed/prediction)<1e-10
            assert abs(datum["native_total_roof_window_vs_single_roof_analytic_relative"]-
                       abs(observed/prediction-1))<1e-10
    scores=e["unaltered_original_250ms_all_adjacent_frozen_three_scores"]
    assert [(z["coarse_ppw"],z["fine_ppw"]) for z in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,q in enumerate(scores):
        true=compare_complex_transfer(
            reference=rows[i+1]["original_full_record_250ms_signed_40_80_replayed"],
            candidate=rows[i]["original_full_record_250ms_signed_40_80_replayed"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(q["canonical_original_full_unfiltered_250ms_P_T_over_Q_T"][key]-true[key])<1e-10
        assert not q["original_full_three_gate_pass"]
        assert q["first_roof_weak_not_canonical_acceptance"]
