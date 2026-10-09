"""Independent direct difference pressure test and original full-q0 leak diagnostics."""
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
from run_r130d_original_pffdtd_native_full_modal_q0 import (
    BANDS,FREQ,stable_sin_ratio,true_native_leapfrog_exact_finite_signed_transfer)
from run_r130d_original_q0_finite_window_endpoint_leakage import (
    TERMS,true_original_pressure_time_parts,validate_plan)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_finite_window_endpoint_leakage_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_finite_window_endpoint_leakage_evidence_2026-10-09.json"
PREV=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


@pytest.mark.parametrize("dt,nt",[(.001,8),(.0002,13),(.00014,103)])
def test_independent_direct_discrete_pffdtd_wave_time_pressure_all_parts(dt,nt):
    # Evaluate EVERY pressure sample from an independent explicit real wave,
    # including Neumann theta=0. Derive p[0] and p[N-1] directly from phi.
    theta=np.array([0.,.02,.45,1.0,2.2,2.8],dtype=float)
    amplitude=np.array([1.3,-1.7,.8,-.56,2.1,-.001],dtype=float)
    rho=1.2
    wave=np.stack([amplitude*stable_sin_ratio(i,theta)
                   for i in range(nt)])
    pressure=np.empty_like(wave)
    pressure[0]=rho*(-3*wave[0]+4*wave[1]-wave[2])/(2*dt)
    pressure[1:-1]=rho*(wave[2:]-wave[:-2])/(2*dt)
    pressure[-1]=rho*(3*wave[-1]-4*wave[-2]+wave[-3])/(2*dt)
    answer=true_original_pressure_time_parts(theta,amplitude,dt,nt,rho)
    for j,indices in enumerate(([0],list(range(1,nt-1)),[nt-1])):
        n=np.array(indices)
        for i,f in enumerate(FREQ):
            original=np.sum(pressure[n]*np.exp(2j*np.pi*f*dt*n[:,None]),axis=0)
            np.testing.assert_allclose(answer[j,i],original,rtol=3e-10,atol=2e-8)
    exact=true_native_leapfrog_exact_finite_signed_transfer(theta,amplitude,dt,nt,rho)
    np.testing.assert_allclose(answer.sum(axis=0),exact,rtol=3e-10,atol=1e-7)


@pytest.mark.parametrize("bad",["time_terms","all_3d_original_modes_no_exclusion"])
def test_no_alternate_pressure_or_filtered_modes_allowed(bad):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    if bad=="time_terms":p["decomposition"][bad]=["centered_interior_n1_to_Nminus2"]
    else:p["decomposition"][bad]=False
    with pytest.raises(ValueError,match="plan mutated"):
        validate_plan(p)


@pytest.mark.parametrize("name,value",[
    ("original_frozen_complex_limit",1.0),
    ("original_frozen_magnitude_limit",1.0),
    ("original_frozen_phase_deg",180)])
def test_canonical_limits_fail_closed(name,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["frozen"][name]=value
    with pytest.raises(ValueError,match="plan mutated"):
        validate_plan(p)


def test_frozen_original_q0_five_grids_and_complete_mode_time_band_partition():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    archived=json.loads(PREV.read_text(encoding="utf-8"))
    raw=json.loads(RAW.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_upstream_pffdtd_runs"]==e["new_github_actions_runs"]==0
    assert e["actual_original_full_nongated_point_q0_retains_same_failure"]
    assert e["diagnostic_components_are_not_alternate_pressure_transfer_calibrations"]
    cases=e["actual_original_full_3D_all_modal_time_parts"]
    original={x["ppw"]:x for x in archived["actual_original_unmodified_all_mode_native_cases"]}
    actual={x["ppw"]:x for x in raw["actual_native_wave_cases"]}
    assert [q["ppw"] for q in cases]==list(PPW)
    assert sum(q["actual_original_modes_in_full_3D_graph"] for q in cases)==347154
    for c in cases:
        ppw=c["ppw"]
        assert c["native_original_points_waveform_and_full_record_unchanged"]
        assert c["actual_original_modes_in_full_3D_graph"]==original[ppw]["actual_native_full_3D_modes_count"]
        assert c["original_native_voxel_SHA256"]==original[ppw]["original_native_voxels_SHA256"]
        assert c["original_native_comms_SHA256"]==actual[ppw]["original_native_comm_sha256"]
        assert c["native_Nt"]==original[ppw]["original_record_samples"]
        assert c["native_dt_s"]==original[ppw]["original_native_time_step_s"]
        assert c["native_pffdtd_all_modes_vs_true_original_raw_wave_complex_rel"]<2e-6
        assert c["three_exact_time_terms_vs_previous_original_single_formula_rel"]<2e-10
        whole=unpairs(c["original_native_full_250ms_signed_transfer_40_80"])
        archived_signal=unpairs(original[ppw]["exact_original_saved_true_PFFDTD_q0_signed_40_80"])
        assert np.linalg.norm(whole-archived_signal)/max(np.linalg.norm(archived_signal),1e-15)<2e-6
        timeparts=c["original_full_record_time_terms_signed_40_80"]
        assert set(timeparts)==set(TERMS)
        assert np.allclose(sum((unpairs(timeparts[n]) for n in TERMS),
                               np.zeros(2,dtype=complex)),whole,rtol=2e-10,atol=1e-8)
        bands=c["original_native_five_frequency_bands_all_time_terms"]
        assert [v["semidiscrete_mode_band_hz"] for v in bands]==[list(b) for b in BANDS]
        assert sum(v["all_original_true_modes_count"] for v in bands)==c["actual_original_modes_in_full_3D_graph"]
        for band in bands:
            assert np.allclose(sum((unpairs(band["signed_original_finite_window_time_parts_40_80"][n])
                                    for n in TERMS),np.zeros(2,dtype=complex)),
                               unpairs(band["complete_original_full250ms_signed_40_80"]),
                               rtol=2e-10,atol=1e-7)
    records=e["adjacent_native_q0_parts"]
    assert [(v["original_coarse_ppw"],v["original_fine_ppw"]) for v in records]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,v in enumerate(records):
        assert v["all_mode_and_time_terms_exactly_recombine"]
        assert v["no_alternate_truncated_or_end_modified_score"]
        delta=unpairs(cases[i]["original_native_full_250ms_signed_transfer_40_80"])-unpairs(
            cases[i+1]["original_native_full_250ms_signed_transfer_40_80"])
        assert np.allclose(delta,unpairs(v["original_unmodified_full_signed_40_80_coarse_minus_fine"]))
        pressure=v["actual_original_signed_three_time_region_deltas"]
        assert np.allclose(sum((unpairs(pressure[n]["full_record_original_signed_coarse_minus_fine_40_80"])
                                for n in TERMS),np.zeros(2,dtype=complex)),
                           delta,rtol=2e-10,atol=1e-8)
        bands=v["actual_original_signed_five_band_by_three_time_deltas"]
        assert [a["original_true_mode_frequency_band_hz"] for a in bands]==[list(b) for b in BANDS]
        assert np.allclose(sum((unpairs(q["original_250ms_complete_signed_coarse_minus_fine_40_80"])
                                for q in bands),np.zeros(2,dtype=complex)),
                           delta,rtol=2e-10,atol=1e-8)
        metric=compare_complex_transfer(
            reference=cases[i+1]["original_native_full_250ms_signed_transfer_40_80"],
            candidate=cases[i]["original_native_full_250ms_signed_transfer_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(metric[key]-v["actual_full_original_complex_magnitude_phase_metrics"][key])<1e-10
    # Negative results preserved, never imply boundary-term or high-mode deletion is valid.
    assert all(not (v["actual_full_original_complex_magnitude_phase_metrics"]["complex_rms_relative"]<=.2
                        and v["actual_full_original_complex_magnitude_phase_metrics"]["magnitude_max_relative"]<=.25
                        and v["actual_full_original_complex_magnitude_phase_metrics"]["phase_max_deg"]<=15)
               for v in records)
